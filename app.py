"""
Market Crawler app (Streamlit) with a live spider-on-network-map animation.
Needs universal_market_analyzer.py in the same folder.
Not financial advice.
"""
import math
import time

import streamlit as st
import streamlit.components.v1 as components

import universal_market_analyzer as uma

st.set_page_config(page_title="Market Crawler", page_icon="🕷️", layout="centered")

st.markdown("""
<style>
.log{font-family:monospace;font-size:13px;color:#94a3b8;line-height:1.7;
  background:#0b1220;border-radius:10px;padding:10px 12px;border:1px solid #1e293b}
.log .ok{color:#22c55e}
.big{font-size:44px;font-weight:700;line-height:1.1}
.pill{display:inline-block;padding:4px 12px;border-radius:999px;font-weight:600;font-size:14px}
</style>
""", unsafe_allow_html=True)

# ---------------- spider + network map animation ----------------
NODES = [  # label, x, y  (viewBox 700 x 300)
    ("RESOLVE", 60, 150), ("YAHOO PRICES", 190, 65), ("YAHOO NEWS", 270, 215),
    ("GOOGLE NEWS", 410, 250), ("SENTIMENT", 470, 130), ("PROBABILITY", 580, 205),
    ("RESULT", 640, 70),
]
EDGES = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 6)]
EXTRA = [(0, 2), (1, 4), (2, 4), (3, 5), (4, 6)]           # side links (dim)
DECOR = [(120, 262), (330, 40), (520, 28), (610, 270), (28, 70), (400, 165), (335, 128), (680, 160)]
DECOR_LINKS = [(0, 0), (1, 1), (4, 2), (5, 3), (0, 4), (4, 5), (2, 6), (6, 7)]  # (node idx, decor idx)


def map_html(cur, prev, visited, label):
    out = []
    # dim background mesh
    for ni, di in DECOR_LINKS:
        _, x1, y1 = NODES[ni]
        x2, y2 = DECOR[di]
        out.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#1e293b" stroke-width="1"/>')
    for x, y in DECOR:
        out.append(f'<circle cx="{x}" cy="{y}" r="3" fill="#1e293b"/>')
    for a, b in EXTRA:
        _, x1, y1 = NODES[a]
        _, x2, y2 = NODES[b]
        out.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#243247" stroke-width="1" stroke-dasharray="4 5"/>')
    # main path edges
    for a, b in EDGES:
        _, x1, y1 = NODES[a]
        _, x2, y2 = NODES[b]
        L = math.hypot(x2 - x1, y2 - y1)
        lit = a in visited and b in visited
        fresh = (a == prev and b == cur and prev != cur)
        if fresh:
            out.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#38bdf8" stroke-width="2.5" '
                       f'stroke-dasharray="{L:.0f}" stroke-dashoffset="{L:.0f}" style="animation:draw .9s ease-in-out forwards"/>')
        elif lit:
            out.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#38bdf8" stroke-width="2.5" opacity=".9"/>')
            out.append(f'<circle r="3.5" fill="#e0f2fe"><animateMotion dur="1.6s" repeatCount="indefinite" '
                       f'path="M{x1},{y1} L{x2},{y2}"/></circle>')
        else:
            out.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#334155" stroke-width="2"/>')
    # nodes
    for i, (name, x, y) in enumerate(NODES):
        on = i in visited
        col = "#22c55e" if on else "#475569"
        fill = "#052e16" if on else "#0f172a"
        if i == cur:
            out.append(f'<circle cx="{x}" cy="{y}" r="12" fill="none" stroke="#38bdf8" stroke-width="2">'
                       f'<animate attributeName="r" values="12;24;12" dur="1.4s" repeatCount="indefinite"/>'
                       f'<animate attributeName="opacity" values="1;0;1" dur="1.4s" repeatCount="indefinite"/></circle>')
        out.append(f'<circle cx="{x}" cy="{y}" r="10" fill="{fill}" stroke="{col}" stroke-width="2.5"/>')
        if on:
            out.append(f'<text x="{x}" y="{y+4}" text-anchor="middle" font-size="11" fill="#22c55e">✓</text>')
        out.append(f'<text x="{x}" y="{y+28}" text-anchor="middle" font-size="10" '
                   f'font-family="monospace" fill="{"#cbd5e1" if on else "#64748b"}">{name}</text>')
    # spider (walks from previous node to current node)
    px, py = NODES[prev][1], NODES[prev][2]
    cx, cy = NODES[cur][1], NODES[cur][2]
    out.append(f'<g style="transform:translate({cx}px,{cy}px);animation:mv .9s ease-in-out;">'
               f'<text text-anchor="middle" dy="9" font-size="30">🕷️</text></g>')
    keyframes = (f"@keyframes mv{{from{{transform:translate({px}px,{py}px)}}to{{transform:translate({cx}px,{cy}px)}}}}"
                 "@keyframes draw{to{stroke-dashoffset:0}}"
                 "@keyframes grid{to{background-position:28px 28px}}")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
{keyframes}
body{{margin:0;background:#0b1220;font-family:monospace}}
.box{{border:1px solid #1e293b;border-radius:12px;overflow:hidden;
 background:#0b1220 repeating-linear-gradient(0deg,transparent 0 27px,rgba(56,189,248,.07) 27px 28px),
 repeating-linear-gradient(90deg,transparent 0 27px,rgba(56,189,248,.07) 27px 28px);
 animation:grid 2.5s linear infinite}}
.lab{{color:#7dd3fc;font-size:13px;padding:8px 12px 0}}
.lab:after{{content:"_";animation:b 1s steps(1) infinite}}@keyframes b{{50%{{opacity:0}}}}
svg{{width:100%;height:auto;display:block}}
</style></head><body><div class="box"><div class="lab">{label}</div>
<svg viewBox="0 0 700 300" xmlns="http://www.w3.org/2000/svg">{''.join(out)}</svg></div></body></html>"""


def draw_map(slot, cur, prev, visited, label):
    with slot.container():
        components.html(map_html(cur, prev, visited, label), height=330)


# ---------------- UI ----------------
st.title("🕷️ Market Crawler")
st.caption("A spider crawls prices and news, scores sentiment, and estimates probabilities. Not financial advice.")

assets = st.text_input("Assets (comma separated)", "bitcoin, AAPL, gold, eurusd",
                       help="Tickers or names: AAPL, 7203.T, bitcoin, gold, eurusd, nasdaq")
with st.expander("Settings"):
    c1, c2 = st.columns(2)
    horizon = c1.slider("Forecast horizon (days)", 1, 10, 1)
    days = c2.slider("Price history (days)", 30, 180, 60)
    news_n = c1.slider("Max headlines", 5, 40, 20)
    region = c2.text_input("News region", "US")

go = st.button("🚀 Start crawling", use_container_width=True, type="primary")


def verdict_color(v):
    return {"BULLISH": "#16a34a", "SLIGHTLY BULLISH": "#65a30d", "NEUTRAL": "#6b7280",
            "SLIGHTLY BEARISH": "#ea580c", "BEARISH": "#dc2626"}[v]


def crawl(raw, http, anim, log_box, bar, base, share):
    lines = []
    state = {"prev": 0, "visited": set()}

    def step(msg, frac, node):
        prev = state["prev"]
        state["visited"].add(node)
        draw_map(anim, node, prev, state["visited"], msg)
        lines.append(f"<span class='ok'>▸</span> {msg}")
        log_box.markdown("<div class='log'>" + "<br>".join(lines[-6:]) + "</div>", unsafe_allow_html=True)
        bar.progress(min(1.0, base + share * frac))
        state["prev"] = node
        time.sleep(1.0 if node != prev else 0.3)

    sym = uma.resolve(raw)
    step(f"Resolving {raw} → {sym} ({uma.asset_type(sym)})", 0.1, 0)
    step("Crawling Yahoo Finance price history", 0.25, 1)
    closes, meta = uma.get_prices(http, sym, days)
    if len(closes) < 10:
        step("Not enough price data (check symbol or retry in a minute)", 1, state["prev"])
        return None
    step(f"Got {len(closes)} daily closes for {meta.get('name', sym)}", 0.45, 1)
    step("Crawling Yahoo Finance news", 0.55, 2)
    q = raw if uma.asset_type(sym) == "forex" else meta.get("name", raw)
    items = uma.get_news(http, sym, q, region, "en", news_n)
    step(f"Crawling Google News, {len(items)} unique headlines", 0.7, 3)
    s, conf = uma.news_score(items)
    step("Scoring headline sentiment", 0.85, 4)
    res = uma.model(closes, s, conf, horizon)
    step("Computing probabilities", 0.95, 5)
    res.update({"symbol": sym, "type": uma.asset_type(sym), **meta, "news_sentiment": s,
                "news_confidence": conf, "verdict": uma.verdict(res["p_up"])})
    step("Done: result ready", 1.0, 6)
    return res, closes, items


def show(res, closes, items):
    col = verdict_color(res["verdict"])
    st.markdown(f"### {res['name']}  `{res['symbol']}`")
    st.markdown(
        f"<span class='pill' style='background:{col}22;color:{col};border:1px solid {col}'>"
        f"{res['verdict']}</span>", unsafe_allow_html=True)
    a, b = st.columns(2)
    a.markdown(f"<div class='big' style='color:{col}'>{res['p_up']:.0%}</div>"
               f"<div>chance of up move<br>({res['horizon_days']}d horizon)</div>", unsafe_allow_html=True)
    b.metric("Price", f"{res['price']:,.4g} {res['currency'] or ''}")
    st.progress(float(res["p_up"]))
    m1, m2, m3 = st.columns(3)
    m1.metric("P(gain > %.1f%%)" % res["threshold_pct"], f"{res['p_gain_over_thr']:.0%}")
    m2.metric("P(loss > %.1f%%)" % res["threshold_pct"], f"{res['p_loss_over_thr']:.0%}")
    m3.metric("Volatility/day", f"{res['daily_vol_pct']:.2f}%")
    st.caption(f"Likely range (68%): {res['range_68pct'][0]:+.2f}% to {res['range_68pct'][1]:+.2f}% · "
               f"news {res['news_sentiment']:+.2f} (confidence {res['news_confidence']:.0%}) · "
               f"momentum {res['momentum']:+.2f} · base rate {res['base_rate']:.0%}")
    st.line_chart(closes, height=180)
    if items:
        with st.expander(f"Headlines crawled ({len(items)})"):
            for it in items:
                sc = uma.sentiment(it["title"])
                dot = "🟢" if sc > 0.15 else "🔴" if sc < -0.15 else "⚪"
                st.markdown(f"{dot} `{sc:+.2f}` [{it['title']}]({it.get('link') or ''}) · _{it['source']}_")


if go:
    names = [x.strip() for x in assets.split(",") if x.strip()]
    if not names:
        st.warning("Enter at least one asset.")
        st.stop()
    http = uma.Http()
    anim = st.empty()
    bar = st.progress(0.0)
    log_box = st.empty()
    results = []
    for i, raw in enumerate(names):
        out = crawl(raw, http, anim, log_box, bar, i / len(names), 1 / len(names))
        if out:
            results.append(out)
    anim.empty()
    bar.empty()
    log_box.empty()
    if not results:
        st.error("Nothing could be crawled. Check the symbols or try again in a minute.")
    for res, closes, items in results:
        st.divider()
        show(res, closes, items)
    if results:
        st.success(f"Crawled {len(results)} asset(s) ✔")
else:
    draw_map(st.empty(), 0, 0, {0}, "Ready. Tap Start crawling")
