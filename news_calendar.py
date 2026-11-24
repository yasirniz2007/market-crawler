"""
News & calendar crawler: the same spider-over-a-document animation, run over live
forex headlines and this week's economic calendar.

Sources (public feeds, no page scraping):
  - FXStreet news RSS, plus any RSS/Atom feed URL you add
  - Forex Factory's weekly calendar JSON feed (unofficial, may rate limit or change)
  - TradingView: official free embeddable widgets (chart + economic calendar).
    TradingView and the Forex Factory site have no public data API and block
    scrapers, so they are embedded or read through public feeds only.

Needs report_crawler.py and universal_market_analyzer.py in the same folder.
Not financial advice.
"""
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone

import requests
import streamlit as st
import streamlit.components.v1 as components

import report_crawler as rc
import universal_market_analyzer as uma

UA = "Mozilla/5.0 (compatible; MarketCrawler/1.0)"
DEFAULT_FEEDS = ["https://www.fxstreet.com/rss/news"]
CAL_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CURRENCIES = ["All", "USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"]


# ---------------------------------------------------------------- fetching
def parse_feed(content, tag):
    """RSS 2.0 and Atom, whichever the feed uses."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    out = []
    for el in root.iter():
        if el.tag.split("}")[-1] not in ("item", "entry"):
            continue
        title = link = date = ""
        for ch in el:
            n = ch.tag.split("}")[-1]
            if n == "title":
                title = (ch.text or "").strip()
            elif n == "link":
                link = (ch.text or "").strip() or ch.attrib.get("href", "")
            elif n in ("pubDate", "published", "updated"):
                date = (ch.text or "").strip()
        if title:
            out.append({"title": title, "link": link, "published": date, "source": tag})
    return out


@st.cache_data(ttl=600, show_spinner=False)
def fetch_feed(url):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
        host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        return parse_feed(r.content, host), ""
    except Exception as e:
        return [], f"{url}: {e}"


@st.cache_data(ttl=900, show_spinner=False)
def fetch_calendar():
    try:
        r = requests.get(CAL_URL, headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
        data = r.json()
        return (data if isinstance(data, list) else []), ""
    except Exception as e:
        return [], f"calendar feed: {e}"


# ---------------------------------------------------------------- payload
def _when(ev):
    try:
        return datetime.fromisoformat(ev["date"]).astimezone(timezone.utc)
    except Exception:
        return None


def build_payload(events, items, ccy, impacts):
    chosen = [e for e in events if e.get("impact") in impacts and (ccy == "All" or e.get("country") == ccy)]
    chosen.sort(key=lambda e: (_when(e) or datetime.max.replace(tzinfo=timezone.utc)))
    with_data = [e for e in chosen if e.get("forecast") or e.get("previous")]
    shown = (with_data or chosen)[:7]

    lines, seq, rows = [], [], []

    def txt(t, k="txt"):
        lines.append({"t": t, "h": None, "k": k})

    def fig(key, label, value):
        t = f"{label} {value}"
        a = len(label) + 1
        lines.append({"t": t, "h": [a, a + len(value)], "k": "num"})
        seq.append({"line": len(lines) - 1, "key": key})

    n_high = sum(1 for e in events if e.get("impact") == "High" and (ccy == "All" or e.get("country") == ccy))
    txt("ECONOMIC CALENDAR AND NEWS", "hdr")
    txt(f"Currency: {ccy}. {len(chosen)} events match, {n_high} high impact. Times in UTC.")
    txt("UPCOMING EVENTS", "hdr")
    for i, e in enumerate(shown):
        w = _when(e)
        day = w.strftime("%a %d %b %H:%M UTC") if w else str(e.get("date", ""))[:16]
        name = f"{e.get('country', '')} {e.get('title', '')}".strip()
        use_fc = bool(e.get("forecast"))
        value = e.get("forecast") or e.get("previous") or "n/a"
        fig(f"e{i}", f"{name[:44]} {'forecast' if use_fc else 'previous'}", value)
        rows.append({"key": f"e{i}", "label": name[:30], "section": "EVENTS", "found": True,
                     "cur": value, "prior": e.get("previous") or "", "note": f"{e.get('impact', '')} · {day}"})

    scores = [uma.sentiment(it["title"]) for it in items]
    avg = sum(scores) / len(scores) if scores else 0.0
    txt("HEADLINES", "hdr")
    fig("news", "Average headline sentiment", f"{avg:+.2f}")
    rows.append({"key": "news", "label": "Headline sentiment", "section": "NEWS", "found": True,
                 "cur": f"{avg:+.2f}", "prior": "", "note": f"{len(items)} headlines"})
    for it, s in list(zip(items, scores))[:6]:
        txt(f"{s:+.2f} {it['title'][:88]}")

    busiest = Counter(_when(e).strftime("%a") for e in chosen if e.get("impact") == "High" and _when(e))
    bull = sum(1 for s in scores if s > 0.15)
    bear = sum(1 for s in scores if s < -0.15)
    calc = [{"label": "High-impact events", "cur": str(n_high), "prior": "", "note": "this week"}]
    if busiest:
        d, c = busiest.most_common(1)[0]
        calc.append({"label": "Busiest day", "cur": f"{d} ({c})", "prior": "", "note": "high-impact count"})
    if scores:
        calc.append({"label": "Bullish vs bearish", "cur": f"{bull} / {bear}", "prior": "", "note": "headlines scored"})

    title = f"Forex calendar & news · {ccy}"
    return {"tag": f"{ccy} WEEK", "total": len(rows), "lines": lines, "rows": rows, "seq": seq,
            "meta": title, "title": title, "calc": calc, "cols": ["FIGURE", "FORECAST", "PREVIOUS", "NOTE"]}


# ---------------------------------------------------------------- TradingView (official embeds)
def tv_widget(script, config, height):
    cfg = json.dumps(config).replace("</", "<\\/")
    html = (f'<div class="tradingview-widget-container" style="height:{height}px;width:100%">'
            f'<div class="tradingview-widget-container__widget" style="height:calc(100% - 24px);width:100%"></div>'
            f'<script type="text/javascript" '
            f'src="https://s3.tradingview.com/external-embedding/{script}" async>{cfg}</script></div>')
    components.html(html, height=height)


# ---------------------------------------------------------------- UI
def render():
    st.markdown("### 🗞️ News & calendar crawler")
    st.caption("Crawls live forex headlines and this week's economic calendar from public feeds. "
               "TradingView is shown through its official free widgets below.")
    c1, c2 = st.columns(2)
    ccy = c1.selectbox("Currency", CURRENCIES, key="nc_ccy")
    impact = c2.selectbox("Impact", ["High", "High + Medium"], key="nc_imp")
    extra = st.text_area("Extra RSS / Atom feed URLs (one per line, optional)", key="nc_feeds",
                         placeholder="https://example.com/feed.xml")
    go = st.button("🕷️ Crawl news & calendar", type="primary", use_container_width=True, key="nc_go")

    if go:
        urls = DEFAULT_FEEDS + [u.strip() for u in extra.splitlines() if u.strip().startswith("http")]
        errors, items = [], []
        with st.status("Crawling…", expanded=True) as status:
            st.write("▸ Reading the weekly economic calendar feed")
            events, err = fetch_calendar()
            if err:
                errors.append(err)
            for u in urls:
                st.write(f"▸ Reading {u}")
                got, err = fetch_feed(u)
                items += got
                if err:
                    errors.append(err)
            status.update(label=f"Done: {len(events)} events, {len(items)} headlines", state="complete")
        for e in errors:
            st.warning(f"Could not read {e}. The site may be blocking cloud servers or rate limiting; retry later.")
        if not events and not items:
            st.error("Nothing came back from any feed.")
        else:
            impacts = {"High"} if impact == "High" else {"High", "Medium"}
            st.session_state["nc_report"] = build_payload(events, items, ccy, impacts)

    payload = st.session_state.get("nc_report")
    if payload:
        st.caption(payload["meta"])
        components.html(rc.animation_html(payload), height=900, scrolling=False)
        rows = [{"Figure": r["label"], "Forecast": r["cur"], "Previous": r["prior"], "Note": r["note"]}
                for r in payload["rows"]]
        rows += [{"Figure": c["label"], "Forecast": c["cur"], "Previous": "", "Note": "calculated: " + c["note"]}
                 for c in payload["calc"]]
        st.dataframe(rows, use_container_width=True, hide_index=True)

    st.markdown("#### 📈 TradingView (official embeds)")
    sym = st.text_input("TradingView symbol", "FX:EURUSD", key="nc_sym",
                        help="Examples: FX:EURUSD, OANDA:XAUUSD, BINANCE:BTCUSDT, NASDAQ:AAPL, TVC:DXY")
    tv_widget("embed-widget-advanced-chart.js",
              {"autosize": True, "symbol": sym.strip() or "FX:EURUSD", "interval": "60", "timezone": "Etc/UTC",
               "theme": "dark", "style": "1", "locale": "en", "allow_symbol_change": True,
               "support_host": "https://www.tradingview.com"}, 520)
    tv_widget("embed-widget-events.js",
              {"colorTheme": "dark", "isTransparent": False, "width": "100%", "height": "100%", "locale": "en",
               "importanceFilter": "-1,0,1", "currencyFilter": "USD,EUR,GBP,JPY,AUD,CAD,CHF,NZD"}, 520)
    st.caption("Feeds can be delayed, rate limited or change without notice. Check important data at the source. "
               "Not financial advice.")
