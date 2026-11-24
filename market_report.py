"""
Any-market crawler: the same spider-over-a-document animation as report_crawler.py,
but for ANY market (crypto, forex, commodities, indices, stocks, any country).

There are no SEC filings for most of these, so the crawler builds a live
"market snapshot" page from Yahoo Finance daily closes plus Google/Yahoo news
headlines, then the spider crawls that page and pulls out the key figures.
Needs report_crawler.py and universal_market_analyzer.py in the same folder.
Not financial advice.
"""
import math

import streamlit as st
import streamlit.components.v1 as components

import report_crawler as rc
import universal_market_analyzer as uma

CATEGORIES = {
    "Crypto": ["bitcoin", "ethereum", "solana", "xrp", "dogecoin", "bnb"],
    "Forex": ["eurusd", "gbpusd", "usdjpy", "audusd", "usdchf", "usdcad"],
    "Commodities": ["gold", "silver", "oil", "brent", "copper", "gas"],
    "Indices": ["nasdaq", "sp500", "dow", "dax", "nikkei", "ftse"],
    "Stocks": ["AAPL", "MSFT", "NVDA", "TSLA", "GS", "7203.T"],
}


def _num(v):
    a = abs(v)
    if a >= 100:
        return f"{v:,.2f}"
    if a >= 0.01:
        return f"{v:,.4f}"
    return f"{v:.6f}"


def _pct(x):
    return f"{x:+.2f}%"


def build_market_payload(raw, sym, kind, closes, meta, items, res, s, horizon):
    name = meta.get("name") or sym
    src = meta.get("source", "Yahoo Finance")
    cur = meta.get("currency") or ""
    last = closes[-1]
    price = meta.get("price") or last
    n30 = min(30, len(closes))
    window = closes[-n30:]
    hi, lo = max(window), min(window)

    def chg(n):
        base = closes[-1 - n] if len(closes) > n else closes[0]
        return (last / base - 1) * 100 if base else 0.0

    d1, d5, d30 = chg(1), chg(5), chg(n30 - 1 if n30 > 1 else 1)
    vol = res["daily_vol_pct"]

    lines, seq = [], []

    def txt(t, k="txt"):
        lines.append({"t": t, "h": None, "k": k})

    def fig(key, label, value):
        t = f"{label} {value}"
        a = len(label) + 1
        lines.append({"t": t, "h": [a, a + len(value)], "k": "num"})
        seq.append({"line": len(lines) - 1, "key": key})

    txt("MARKET SNAPSHOT", "hdr")
    txt(f"{name} ({sym})")
    txt(f"Asset type: {kind}. Source: {src} daily closes, {len(closes)} sessions.")
    txt("PRICE", "hdr")
    fig("last", f"Last price ({cur or 'quote'})", _num(price))
    fig("d1", "Change over 1 day", _pct(d1))
    fig("d5", "Change over 5 days", _pct(d5))
    fig("d30", f"Change over {n30} days", _pct(d30))
    fig("hi", f"{n30}-day high", _num(hi))
    fig("lo", f"{n30}-day low", _num(lo))
    txt("RISK", "hdr")
    fig("vol", "Volatility per day", f"{vol:.2f}%")
    txt("NEWS", "hdr")
    fig("news", "News sentiment score", f"{s:+.2f}")
    for it in items[:6]:
        txt(f"{uma.sentiment(it['title']):+.2f} {it['title'][:88]}")
    txt("MODEL", "hdr")
    fig("mom", "Momentum score", f"{res['momentum']:+.2f}")
    fig("pup", f"Chance of up move ({horizon}d)", f"{res['p_up']:.0%}")
    txt(f"Verdict: {uma.verdict(res['p_up'])}. Rough statistical estimate, not advice.")

    def row(key, label, section, cur_v, prior="", note=None):
        note = note or f"live · {src}"
        return {"key": key, "label": label, "section": section, "found": True,
                "cur": cur_v, "prior": prior, "note": note}

    prev = closes[-2] if len(closes) > 1 else last
    rows = [
        row("last", "Last price", "PRICE", _num(price), _num(prev), "prior = previous close"),
        row("d1", "Change 1 day", "PRICE", _pct(d1)),
        row("d5", "Change 5 days", "PRICE", _pct(d5)),
        row("d30", f"Change {n30} days", "PRICE", _pct(d30)),
        row("hi", f"{n30}-day high", "PRICE", _num(hi)),
        row("lo", f"{n30}-day low", "PRICE", _num(lo)),
        row("vol", "Volatility / day", "RISK", f"{vol:.2f}%", "", "std dev of daily returns"),
        row("news", "News sentiment", "SIGNALS", f"{s:+.2f}", "", f"{len(items)} headlines"),
        row("mom", "Momentum", "SIGNALS", f"{res['momentum']:+.2f}", "", "trend strength, -1 to +1"),
        row("pup", f"Chance of up move ({horizon}d)", "SIGNALS", f"{res['p_up']:.0%}", "",
            f"base rate {res['base_rate']:.0%}"),
    ]

    calc = [
        {"label": "From period high", "cur": _pct((last / hi - 1) * 100), "prior": "", "note": "price vs high"},
        {"label": "From period low", "cur": _pct((last / lo - 1) * 100), "prior": "", "note": "price vs low"},
    ]
    if hi > lo:
        calc.append({"label": "Range position", "cur": f"{(last - lo) / (hi - lo):.0%}", "prior": "",
                     "note": "0% = at low, 100% = at high"})
    if vol > 0:
        calc.append({"label": "Move vs normal swing", "cur": f"{d30 / (vol * math.sqrt(max(n30 - 1, 1))):+.2f}x",
                     "prior": "", "note": "period move / typical swing"})

    title = f"{name} · {sym} · {kind}"
    return {"tag": f"{sym} LIVE", "total": len(rows), "lines": lines, "rows": rows, "seq": seq,
            "meta": title, "title": title, "calc": calc, "cols": ["FIGURE", "NOW", "PREVIOUS", "NOTE"]}


def render():
    st.markdown("### 🌐 Any market crawler")
    st.caption("Pick a market or type any ticker. The spider crawls live prices and news and pulls out the key "
               "figures. Works for crypto, forex, commodities, indices and stocks worldwide.")
    c1, c2 = st.columns(2)
    cat = c1.selectbox("Market", list(CATEGORIES), key="mk_cat")
    pick = c2.selectbox("Asset", CATEGORIES[cat], key="mk_pick")
    custom = st.text_input("Or type any ticker / name (overrides the list)", key="mk_custom",
                           placeholder="e.g. RELIANCE.NS, 7203.T, ETH/USD, nikkei, gold")
    with st.expander("Settings"):
        a, b = st.columns(2)
        days = a.slider("Price history (days)", 30, 180, 60, key="mk_days")
        horizon = b.slider("Forecast horizon (days)", 1, 10, 1, key="mk_hor")
        news_n = a.slider("Max headlines", 5, 30, 12, key="mk_news")
        region = b.text_input("News region", "US", key="mk_region")
    go = st.button("🕷️ Crawl market", type="primary", use_container_width=True, key="mk_go")

    if go:
        raw = custom.strip() or pick
        sym = uma.resolve(raw)
        kind = uma.asset_type(sym)
        try:
            with st.status("Crawling…", expanded=True) as status:
                http = uma.Http()
                st.write(f"▸ Resolving {raw} → {sym} ({kind})")
                st.write("▸ Crawling live price history (Yahoo, with backup sources)")
                closes, meta = uma.get_prices(http, sym, days)
                if len(closes) < 10:
                    why = " | ".join(http.notes[-4:]) or "no reason reported"
                    raise RuntimeError("Not enough price data. Tried Yahoo and backup sources. " + why)
                st.write(f"▸ Got {len(closes)} daily closes from {meta.get('source', 'Yahoo Finance')}")
                st.write("▸ Crawling news headlines")
                q = raw if kind == "forex" else meta.get("name", raw)
                items = uma.get_news(http, sym, q, region, "en", news_n)
                s, conf = uma.news_score(items)
                res = uma.model(closes, s, conf, horizon)
                st.write(f"▸ Scored {len(items)} headlines and built the snapshot")
                status.update(label="Done", state="complete")
            st.session_state["mk_report"] = build_market_payload(raw, sym, kind, closes, meta, items, res, s, horizon)
        except Exception as e:
            st.error(str(e))

    payload = st.session_state.get("mk_report")
    if not payload:
        return
    st.caption(payload["meta"])
    components.html(rc.animation_html(payload), height=900, scrolling=False)
    rows = [{"Figure": r["label"], "Now": r["cur"], "Previous": r["prior"], "Note": r["note"]}
            for r in payload["rows"]]
    rows += [{"Figure": c["label"], "Now": c["cur"], "Previous": c["prior"], "Note": "calculated: " + c["note"]}
             for c in payload["calc"]]
    st.dataframe(rows, use_container_width=True, hide_index=True)
    st.caption("Data from Yahoo Finance and public news feeds, which can be delayed or rate limited. "
               "Not financial advice.")
