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
from urllib.parse import quote_plus, urlparse

import requests

UA = "UniversalMarketAnalyzer/1.0 (educational; contact: you@example.com)"

# ---------- friendly name -> Yahoo symbol ----------
ALIASES = {
    "bitcoin": "BTC-USD", "btc": "BTC-USD", "ethereum": "ETH-USD", "eth": "ETH-USD",
    "solana": "SOL-USD", "xrp": "XRP-USD", "dogecoin": "DOGE-USD", "bnb": "BNB-USD",
    "gold": "GC=F", "silver": "SI=F", "oil": "CL=F", "crude": "CL=F", "brent": "BZ=F",
    "gas": "NG=F", "copper": "HG=F",
    "xauusd": "GC=F", "xau": "GC=F", "xagusd": "SI=F",
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
        self.s.headers["User-Agent"] = UA
        self.delay, self._last, self._rp = delay, 0.0, {}

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
                    time.sleep(5 * i); continue
                r.raise_for_status()
                return r
            except requests.RequestException:
                time.sleep(2 * i)
        return None


# ---------- data ----------
def get_prices(http, sym, days):
    rng = "6mo" if days <= 180 else "2y"
    r = http.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                 params={"range": rng, "interval": "1d"})
    try:
        res = r.json()["chart"]["result"][0]
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None][-days:]
        m = res["meta"]
        return closes, {"name": m.get("longName") or m.get("shortName") or sym,
                        "currency": m.get("currency"), "price": m.get("regularMarketPrice")}
    except Exception:
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


FX_POS = (POS - {"easing"}) | {"hawkish", "hike", "hikes", "hiking", "tightening", "strengthens",
                              "strengthen", "firmer", "stronger", "resilient", "upbeat", "hotter"}
FX_NEG = NEG | {"dovish", "easing", "weakens", "weaken", "weaker", "softer", "slides", "slide", "tumbles",
                "tumble", "dips", "dip", "stimulus", "contraction", "stagnation", "pessimism"}


def sentiment(text, fx=False):
    """Headline score in [-1, 1]. fx=True uses central-bank / currency wording."""
    pos_set, neg_set = (FX_POS, FX_NEG) if fx else (POS, NEG)
    w = re.findall(r"[a-z']+", text.lower())
    p = n = 0.0
    for i, x in enumerate(w):
        flip = any(y in NOT for y in w[max(0, i - 2):i])
        if x in pos_set: p, n = (p, n + 1) if flip else (p + 1, n)
        elif x in neg_set: p, n = (p + 1, n) if flip else (p, n + 1)
    return (p - n) / (p + n + 1)


def news_score(items, half_life_h=24.0, fx=False):
    now, num, den = datetime.now(timezone.utc), 0.0, 0.0
    for it in items:
        try:
            age = max(0, (now - parsedate_to_datetime(it["published"])).total_seconds() / 3600)
        except Exception:
            age = 24
        w = 0.5 ** (age / half_life_h)
        num += w * sentiment(it["title"], fx); den += w
    return (num / den if den else 0.0), 1 - math.exp(-den)


# ---------- probability model ----------
phi = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
sigmoid = lambda x: 1 / (1 + math.exp(-x))


def model(closes, s, conf, horizon, w_news=1.5, w_mom=0.6, thr_daily=0.02):
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
    thr = thr_daily * math.sqrt(horizon)
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


# ---------- forex: currency-by-currency news ----------
CUR_QUERY = {
    "USD": "US dollar Federal Reserve", "EUR": "euro ECB eurozone", "GBP": "pound sterling Bank of England",
    "JPY": "Japanese yen Bank of Japan", "CHF": "Swiss franc SNB", "AUD": "Australian dollar RBA",
    "CAD": "Canadian dollar Bank of Canada", "NZD": "New Zealand dollar RBNZ", "INR": "Indian rupee RBI",
    "CNY": "Chinese yuan PBOC", "TRY": "Turkish lira", "BRL": "Brazilian real", "MXN": "Mexican peso",
    "ZAR": "South African rand", "SGD": "Singapore dollar MAS",
}


def pair_currencies(sym):
    c = sym.replace("=X", "")
    if len(c) == 6:
        return c[:3], c[3:]
    if len(c) == 3:
        return "USD", c
    return None, None


def _google(http, query, region, lang, tag, limit):
    q = quote_plus(query + " when:3d")
    items = _rss(http, f"https://news.google.com/rss/search?q={q}&hl={lang}-{region}"
                       f"&gl={region}&ceid={region}:{lang}", "google", tag)
    seen, out = set(), []
    for it in items:
        k = re.sub(r"\W+", "", it["title"].lower())[:60]
        if k and k not in seen:
            seen.add(k); it["tag"] = tag; out.append(it)
    return out[:limit]


def gather_news(http, sym, raw, meta, region, lang, limit):
    """Returns (items, news_score, confidence, fx_info_or_None)."""
    if asset_type(sym) != "forex":
        items = get_news(http, sym, meta.get("name", raw), region, lang, limit)
        for it in items:
            it["tag"] = ""
        s, conf = news_score(items)
        return items, s, conf, None
    base, quote = pair_currencies(sym)
    if not base:
        items = get_news(http, sym, raw, region, lang, limit)
        s, conf = news_score(items, fx=True)
        return items, s, conf, None
    per = max(6, limit // 2)
    bi = _google(http, CUR_QUERY.get(base, base + " currency"), region, lang, base, per)
    qi = _google(http, CUR_QUERY.get(quote, quote + " currency"), region, lang, quote, per)
    di = _google(http, f"{base}/{quote} forex forecast", region, lang, "PAIR", per)
    sb, cb = news_score(bi, fx=True)
    sq, cq = news_score(qi, fx=True)
    sd, cd = news_score(di, fx=True)
    s_cur = max(-1.0, min(1.0, sb - sq))        # base strength minus quote strength
    if di:
        s, conf = 0.6 * s_cur + 0.4 * sd, (cb + cq + cd) / 3
    else:
        s, conf = s_cur, (cb + cq) / 2
    info = {"base": base, "quote": quote, "s_base": sb, "s_quote": sq, "s_direct": sd,
            "n_base": len(bi), "n_quote": len(qi), "n_direct": len(di)}
    return bi + qi + di, s, conf, info


# ---------- scan every forex pair ----------
MAJORS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]
CROSSES = ["EURGBP", "EURJPY", "EURCHF", "EURAUD", "EURCAD", "EURNZD", "GBPJPY", "GBPCHF", "GBPAUD",
           "GBPCAD", "GBPNZD", "AUDJPY", "AUDCHF", "AUDCAD", "AUDNZD", "CADJPY", "CADCHF", "NZDJPY",
           "NZDCHF", "NZDCAD", "CHFJPY"]
ALL_PAIRS = MAJORS + CROSSES          # 28 pairs from the 8 main currencies


def scan_pairs(http, pairs, days, horizon, region, lang, per_cur=10, progress=None):
    """Fast scan: news is fetched once per currency, prices once per pair."""
    cache = {}

    def cur_score(c):
        if c not in cache:
            items = _google(http, CUR_QUERY.get(c, c + " currency"), region, lang, c, per_cur)
            cache[c] = news_score(items, fx=True)
        return cache[c]

    rows = []
    for i, p in enumerate(pairs):
        sym = resolve(p)
        closes, meta = get_prices(http, sym, days)
        b, q = pair_currencies(sym)
        if len(closes) < 10 or not b:
            rows.append({"pair": p, "error": "no price data"})
        else:
            sb, cb = cur_score(b)
            sq, cq = cur_score(q)
            s, conf = max(-1.0, min(1.0, sb - sq)), (cb + cq) / 2
            r = model(closes, s, conf, horizon, thr_daily=0.005)
            rows.append({"pair": f"{b}/{q}", "symbol": sym, "price": meta.get("price"),
                         "p_up": r["p_up"], "verdict": verdict(r["p_up"]), "vol": r["daily_vol_pct"],
                         "base": b, "quote": q, "s_base": sb, "s_quote": sq,
                         "range": r["range_68pct"]})
        if progress:
            progress(i + 1, len(pairs), p)
    strength = {c: v[0] for c, v in cache.items()}
    return {"rows": rows, "strength": strength, "horizon": horizon}


# ---------- main ----------
def analyze(http, raw, a):
    sym = resolve(raw)
    closes, meta = get_prices(http, sym, a.days)
    if len(closes) < 10:
        return {"input": raw, "symbol": sym, "error": "not enough price data (check the symbol)"}
    items, s, conf, fx = gather_news(http, sym, raw, meta, a.region, a.lang, a.news)
    res = model(closes, s, conf, a.horizon, thr_daily=0.005 if fx else 0.02)
    res.update({"input": raw, "symbol": sym, "type": asset_type(sym), **meta, "fx": fx,
                "news_sentiment": s, "news_confidence": conf, "headlines": len(items),
                "verdict": verdict(res["p_up"]),
                "top_headlines": [{"title": i["title"], "score": round(sentiment(i["title"], fx is not None), 2),
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
        if r.get("fx"):
            f = r["fx"]
            print(f"Currency news: {f['base']} {f['s_base']:+.2f} ({f['n_base']}) vs "
                  f"{f['quote']} {f['s_quote']:+.2f} ({f['n_quote']}) | pair headlines {f['s_direct']:+.2f} ({f['n_direct']})")
        for h in r["top_headlines"]:
            print(f"   [{h['score']:+.2f}] {h['title']}")

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"generated": datetime.now(timezone.utc).isoformat(), "results": results}, f, indent=2)
    print(f"\nSaved {a.out}  (not financial advice)")


if __name__ == "__main__":
    main()
