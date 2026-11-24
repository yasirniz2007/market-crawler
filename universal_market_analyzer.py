#!/usr/bin/env python3
"""
Universal market analyzer: ONE file, works for ANY asset.

Assets  : stocks, ETFs, indices, crypto, forex, commodities, any country
          (AAPL, 7203.T, RELIANCE.NS, ^GSPC, BTC-USD, EURUSD=X, GC=F ...)
          Plain names also work: "bitcoin", "gold", "eurusd", "nasdaq".
News    : Yahoo Finance RSS + Google News RSS (any language/region), de-duplicated
Output  : sentiment, momentum, volatility, P(up), P(+/-2% move), verdict, JSON

Install : pip install requests
Run     : python universal_market_analyzer.py bitcoin AAPL gold eurusd 7203.T
          python universal_market_analyzer.py TSLA --days 90 --region IN --horizon 5

NOT financial advice. Probabilities are rough statistical estimates.
"""
import argparse
import json
import math
import re
import time
import urllib.robotparser as robotparser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, quote_plus, urlparse

import requests

UA = "UniversalMarketAnalyzer/1.0 (educational; contact: you@example.com)"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")

# ---------- friendly name -> Yahoo symbol ----------
ALIASES = {
    "bitcoin": "BTC-USD", "btc": "BTC-USD", "ethereum": "ETH-USD", "eth": "ETH-USD",
    "solana": "SOL-USD", "xrp": "XRP-USD", "dogecoin": "DOGE-USD", "bnb": "BNB-USD",
    "gold": "GC=F", "silver": "SI=F", "oil": "CL=F", "crude": "CL=F", "brent": "BZ=F",
    "gas": "NG=F", "copper": "HG=F",
    "sp500": "^GSPC", "s&p500": "^GSPC", "nasdaq": "^IXIC", "dow": "^DJI",
    "dax": "^GDAXI", "nikkei": "^N225", "ftse": "^FTSE", "nifty": "^NSEI", "vix": "^VIX",
}
CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "INR", "CNY", "TRY", "BRL", "MXN", "ZAR", "SGD"}


def resolve(name):
    key = name.strip().lower().replace(" ", "")
    if key in ALIASES:
        return ALIASES[key]
    up = name.strip().upper()
    if re.fullmatch(r"[A-Z]{6}", up) and up[:3] in CURRENCIES and up[3:] in CURRENCIES:
        return up + "=X"                      # eurusd -> EURUSD=X
    if re.fullmatch(r"[A-Z]{2,5}/[A-Z]{3,4}", up):
        return up.replace("/", "-")           # BTC/USD -> BTC-USD
    return up


def asset_type(sym):
    if sym.endswith("=X"): return "forex"
    if sym.endswith("=F"): return "commodity/futures"
    if sym.startswith("^"): return "index"
    if re.search(r"-(USD|EUR|USDT|GBP|JPY)$", sym): return "crypto"
    return "equity/ETF"


# ---------- polite HTTP ----------
class Http:
    def __init__(self, delay=1.2):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": BROWSER_UA, "Accept": "application/json,text/plain,*/*",
                               "Accept-Language": "en-US,en;q=0.9"})
        self.delay, self._last, self._rp = delay, 0.0, {}
        self.notes, self.last_error = [], ""          # why data sources failed (shown in the app)

    def _ok(self, url):
        host = "{0.scheme}://{0.netloc}".format(urlparse(url))
        if host not in self._rp:
            rp = robotparser.RobotFileParser()
            try:
                rp.set_url(host + "/robots.txt"); rp.read()
            except Exception:
                rp = None
            self._rp[host] = rp
        rp = self._rp[host]
        return rp is None or rp.can_fetch(UA, url)

    def get(self, url, retries=3, check_robots=False, **kw):
        if check_robots and not self._ok(url):
            return None
        for i in range(1, retries + 1):
            time.sleep(max(0, self.delay - (time.time() - self._last)))
            try:
                r = self.s.get(url, timeout=15, **kw)
                self._last = time.time()
                if r.status_code == 429:
                    self.last_error = "HTTP 429 (rate limited)"
                    time.sleep(5 * i); continue
                r.raise_for_status()
                self.last_error = ""
                return r
            except requests.RequestException as e:
                resp = getattr(e, "response", None)
                self.last_error = f"HTTP {resp.status_code}" if resp is not None else type(e).__name__
                time.sleep(2 * i)
        return None


# ---------- data ----------
def _yahoo_prices(http, sym, days):
    rng = "6mo" if days <= 180 else "2y"
    for host in ("query1", "query2"):
        r = http.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}", retries=2,
                     params={"range": rng, "interval": "1d"})
        if r is None:
            http.notes.append(f"Yahoo {host}: {http.last_error or 'no reply'}")
            continue
        try:
            res = r.json()["chart"]["result"][0]
            closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None][-days:]
            m = res["meta"]
            return closes, {"name": m.get("longName") or m.get("shortName") or sym,
                            "currency": m.get("currency"), "price": m.get("regularMarketPrice"),
                            "source": "Yahoo Finance"}
        except Exception:
            http.notes.append(f"Yahoo {host}: unexpected reply")
    return [], {}


STOOQ = {"GC=F": "xauusd", "SI=F": "xagusd", "CL=F": "cl.f", "NG=F": "ng.f", "HG=F": "hg.f",
         "^GSPC": "^spx", "^DJI": "^dji", "^GDAXI": "^dax", "^N225": "^nkx", "^FTSE": "^ukx", "^VIX": "^vix"}
COINGECKO = {"BTC-USD": "bitcoin", "ETH-USD": "ethereum", "SOL-USD": "solana", "XRP-USD": "ripple",
             "DOGE-USD": "dogecoin", "BNB-USD": "binancecoin"}


def _stooq_code(sym):
    if sym in STOOQ:
        return STOOQ[sym]
    if sym.endswith("=X"):
        return sym[:-2].lower()                       # EURUSD=X -> eurusd
    if re.fullmatch(r"[A-Z]{1,5}", sym):
        return sym.lower() + ".us"                    # AAPL -> aapl.us
    return None


def _stooq_prices(http, sym, days):
    code = _stooq_code(sym)
    if not code:
        return [], {}
    r = http.get(f"https://stooq.com/q/d/l/?s={quote(code, safe='^.')}&i=d", retries=2)
    if r is None:
        http.notes.append(f"Stooq: {http.last_error or 'no reply'}")
        return [], {}
    rows = r.text.strip().splitlines()
    if len(rows) < 3 or not rows[0].lower().startswith("date"):
        http.notes.append("Stooq: unexpected reply")
        return [], {}
    closes = []
    for ln in rows[1:]:
        p = ln.split(",")
        try:
            closes.append(float(p[4]))
        except (ValueError, IndexError):
            pass
    closes = closes[-days:]
    if not closes:
        return [], {}
    return closes, {"name": sym, "currency": "", "price": closes[-1], "source": "Stooq"}


def _coingecko_prices(http, sym, days):
    cid = COINGECKO.get(sym)
    if not cid:
        return [], {}
    r = http.get(f"https://api.coingecko.com/api/v3/coins/{cid}/market_chart", retries=2,
                 params={"vs_currency": "usd", "days": days, "interval": "daily"})
    if r is None:
        http.notes.append(f"CoinGecko: {http.last_error or 'no reply'}")
        return [], {}
    try:
        closes = [p[1] for p in r.json()["prices"] if p[1] is not None][-days:]
    except Exception:
        http.notes.append("CoinGecko: unexpected reply")
        return [], {}
    if not closes:
        return [], {}
    return closes, {"name": cid.title(), "currency": "USD", "price": closes[-1], "source": "CoinGecko"}


def get_prices(http, sym, days):
    closes, meta = _yahoo_prices(http, sym, days)
    if len(closes) >= 10:
        return closes, meta
    for fn in (_stooq_prices, _coingecko_prices):      # backups when Yahoo blocks cloud servers
        closes, meta = fn(http, sym, days)
        if len(closes) >= 10:
            return closes, meta
    return [], {}


def _rss(http, url, tag, sym):
    r = http.get(url)
    if not r:
        return []
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        return []
    return [{"title": (i.findtext("title") or "").strip(), "link": i.findtext("link"),
             "published": i.findtext("pubDate"), "source": tag} for i in root.iter("item")]


def get_news(http, sym, query, region, lang, limit):
    items = _rss(http, f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={quote_plus(sym)}"
                       f"&region={region}&lang={lang}-{region}", "yahoo", sym)
    q = quote_plus(f"{query} when:7d")
    items += _rss(http, f"https://news.google.com/rss/search?q={q}&hl={lang}-{region}"
                        f"&gl={region}&ceid={region}:{lang}", "google", sym)
    seen, out = set(), []
    for it in items:                      # de-duplicate near-identical titles
        k = re.sub(r"\W+", "", it["title"].lower())[:60]
        if k and k not in seen:
            seen.add(k); out.append(it)
    return out[:limit]


# ---------- sentiment (English lexicon) ----------
POS = {"beat", "beats", "surge", "surges", "soar", "soars", "rally", "rallies", "gain", "gains", "rise",
       "rises", "jump", "jumps", "record", "upgrade", "upgrades", "growth", "profit", "strong", "bullish",
       "outperform", "boost", "boosts", "wins", "approval", "expands", "raises", "optimism", "rebound",
       "climbs", "higher", "breakthrough", "adoption", "inflows", "recovery", "easing"}
NEG = {"miss", "misses", "plunge", "plunges", "fall", "falls", "drop", "drops", "slump", "slumps", "loss",
       "losses", "downgrade", "downgrades", "weak", "bearish", "underperform", "cut", "cuts", "lawsuit",
       "probe", "fraud", "recall", "layoffs", "decline", "declines", "warning", "warns", "fear", "fears",
       "crash", "lower", "investigation", "bankruptcy", "hack", "ban", "outflows", "recession", "sanctions"}
NOT = {"not", "no", "never", "without", "fails", "failed", "despite"}


def sentiment(text):
    w = re.findall(r"[a-z']+", text.lower())
    p = n = 0.0
    for i, x in enumerate(w):
        flip = any(y in NOT for y in w[max(0, i - 2):i])
        if x in POS: p, n = (p, n + 1) if flip else (p + 1, n)
        elif x in NEG: p, n = (p + 1, n) if flip else (p, n + 1)
    return (p - n) / (p + n + 1)


def news_score(items, half_life_h=24.0):
    now, num, den = datetime.now(timezone.utc), 0.0, 0.0
    for it in items:
        try:
            age = max(0, (now - parsedate_to_datetime(it["published"])).total_seconds() / 3600)
        except Exception:
            age = 24
        w = 0.5 ** (age / half_life_h)
        num += w * sentiment(it["title"]); den += w
    return (num / den if den else 0.0), 1 - math.exp(-den)


# ---------- probability model ----------
phi = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
sigmoid = lambda x: 1 / (1 + math.exp(-x))


def model(closes, s, conf, horizon, w_news=1.5, w_mom=0.6):
    r = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    n = len(r)
    mu = sum(r) / n
    sd = math.sqrt(sum((x - mu) ** 2 for x in r) / (n - 1))
    prior = (sum(x > 0 for x in r) + 1) / (n + 2)            # Laplace-smoothed base rate
    t = mu / (sd / math.sqrt(n)) if sd else 0
    mom = math.tanh(t / 2)
    p_up = sigmoid(math.log(prior / (1 - prior)) + w_mom * mom + w_news * conf * s)
    mu_h = (mu + 0.25 * sd * conf * s) * horizon              # drift over horizon
    sd_h = sd * math.sqrt(horizon)                            # volatility scales with sqrt(time)
    thr = 0.02 * math.sqrt(horizon)
    return {
        "p_up": p_up, "base_rate": prior, "momentum": mom, "daily_vol_pct": sd * 100,
        "horizon_days": horizon,
        "p_gain_over_thr": 1 - phi((thr - mu_h) / sd_h) if sd_h else None,
        "p_loss_over_thr": phi((-thr - mu_h) / sd_h) if sd_h else None,
        "threshold_pct": thr * 100,
        "range_68pct": [round((mu_h - sd_h) * 100, 2), round((mu_h + sd_h) * 100, 2)],
    }


def verdict(p):
    return ("BULLISH" if p >= .62 else "SLIGHTLY BULLISH" if p >= .54 else "NEUTRAL" if p > .46
            else "SLIGHTLY BEARISH" if p > .38 else "BEARISH")


# ---------- main ----------
def analyze(http, raw, a):
    sym = resolve(raw)
    closes, meta = get_prices(http, sym, a.days)
    if len(closes) < 10:
        return {"input": raw, "symbol": sym, "error": "not enough price data (check the symbol)"}
    items = get_news(http, sym, meta.get("name", raw) if asset_type(sym) != "forex" else raw,
                     a.region, a.lang, a.news)
    s, conf = news_score(items)
    res = model(closes, s, conf, a.horizon)
    res.update({"input": raw, "symbol": sym, "type": asset_type(sym), **meta,
                "news_sentiment": s, "news_confidence": conf, "headlines": len(items),
                "verdict": verdict(res["p_up"]),
                "top_headlines": [{"title": i["title"], "score": round(sentiment(i["title"]), 2),
                                   "source": i["source"]} for i in items[:3]]})
    return res


def main():
    ap = argparse.ArgumentParser(description="Universal market analyzer")
    ap.add_argument("assets", nargs="+", help="tickers or names: AAPL bitcoin gold eurusd 7203.T")
    ap.add_argument("--days", type=int, default=60, help="price history window (default 60)")
    ap.add_argument("--horizon", type=int, default=1, help="forecast horizon in days (default 1)")
    ap.add_argument("--news", type=int, default=20, help="max headlines per asset")
    ap.add_argument("--region", default="US", help="news region code: US, GB, IN, JP, DE ...")
    ap.add_argument("--lang", default="en", help="news language (sentiment lexicon is English)")
    ap.add_argument("--out", default="universal_result.json")
    a = ap.parse_args()

    http, results = Http(), []
    for raw in a.assets:
        r = analyze(http, raw, a)
        results.append(r)
        if "error" in r:
            print(f"\n{raw}: {r['error']}"); continue
        print(f"\n== {r['name']} [{r['symbol']}, {r['type']}] -> {r['verdict']} ==")
        print(f"Price: {r['price']} {r['currency']}")
        print(f"P(up in {a.horizon}d):        {r['p_up']:.1%}   (base rate {r['base_rate']:.1%})")
        print(f"P(gain > {r['threshold_pct']:.1f}%):      {r['p_gain_over_thr']:.1%}")
        print(f"P(loss > {r['threshold_pct']:.1f}%):      {r['p_loss_over_thr']:.1%}")
        print(f"Likely range (68%):   {r['range_68pct'][0]:+.2f}% to {r['range_68pct'][1]:+.2f}%")
        print(f"News: {r['news_sentiment']:+.2f} (conf {r['news_confidence']:.0%}, {r['headlines']} headlines)"
              f" | momentum {r['momentum']:+.2f} | vol {r['daily_vol_pct']:.2f}%/day")
        for h in r["top_headlines"]:
            print(f"   [{h['score']:+.2f}] {h['title']}")

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"generated": datetime.now(timezone.utc).isoformat(), "results": results}, f, indent=2)
    print(f"\nSaved {a.out}  (not financial advice)")


if __name__ == "__main__":
    main()
