"""
Market Crawler app (Streamlit) with live crawler animation.
Needs universal_market_analyzer.py in the same folder.
Not financial advice.
"""
import time

import streamlit as st

import universal_market_analyzer as uma

st.set_page_config(page_title="Market Crawler", page_icon="🕷️", layout="centered")

CSS = """
<style>
.track{position:relative;height:48px;border-radius:12px;overflow:hidden;
  background:linear-gradient(90deg,#0f172a,#1e293b);border:1px solid #334155;margin:6px 0 10px}
.track:before{content:"";position:absolute;inset:0;
  background:repeating-linear-gradient(90deg,transparent 0 22px,rgba(56,189,248,.14) 22px 24px);
  animation:grid 1s linear infinite}
.scan{position:absolute;top:0;bottom:0;width:70px;
  background:linear-gradient(90deg,transparent,rgba(56,189,248,.4),transparent);
  animation:sweep 1.5s linear infinite}
.bug{position:absolute;top:7px;font-size:28px;animation:run 2.2s ease-in-out infinite alternate}
@keyframes run{from{left:2%}to{left:88%}}
@keyframes sweep{from{left:-70px}to{left:100%}}
@keyframes grid{to{background-position:24px 0}}
.log{font-family:monospace;font-size:13px;color:#94a3b8;line-height:1.7;
  background:#0b1220;border-radius:10px;padding:10px 12px;border:1px solid #1e293b}
.log .ok{color:#22c55e}
.big{font-size:44px;font-weight:700;line-height:1.1}
.pill{display:inline-block;padding:4px 12px;border-radius:999px;font-weight:600;font-size:14px}
</style>
"""
ANIM = '<div class="track"><div class="scan"></div><div class="bug">🕷️</div></div>'

st.markdown(CSS, unsafe_allow_html=True)
st.title("🕷️ Market Crawler")
st.caption("Crawls prices and news, scores sentiment, and estimates probabilities. Not financial advice.")

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

    def step(msg, frac):
        lines.append(f"<span class='ok'>▸</span> {msg}")
        log_box.markdown("<div class='log'>" + "<br>".join(lines[-7:]) + "</div>", unsafe_allow_html=True)
        bar.progress(min(1.0, base + share * frac))
        time.sleep(0.35)

    sym = uma.resolve(raw)
    step(f"Resolving <b>{raw}</b> → {sym} ({uma.asset_type(sym)})", 0.1)
    step("Crawling Yahoo Finance price history…", 0.25)
    closes, meta = uma.get_prices(http, sym, days)
    if len(closes) < 10:
        step("✖ Not enough price data (check the symbol or retry in a minute)", 1)
        return None
    step(f"Got {len(closes)} daily closes for {meta.get('name', sym)}", 0.45)
    step("Crawling Yahoo + Google News headlines…", 0.6)
    q = raw if uma.asset_type(sym) == "forex" else meta.get("name", raw)
    items = uma.get_news(http, sym, q, region, "en", news_n)
    step(f"Collected {len(items)} unique headlines", 0.75)
    step("Scoring sentiment (recency weighted)…", 0.85)
    s, conf = uma.news_score(items)
    step("Computing probabilities (momentum + volatility + news)…", 0.95)
    res = uma.model(closes, s, conf, horizon)
    res.update({"symbol": sym, "type": uma.asset_type(sym), **meta, "news_sentiment": s,
                "news_confidence": conf, "verdict": uma.verdict(res["p_up"])})
    step("Done ✔", 1.0)
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
                link = it.get("link") or ""
                st.markdown(f"{dot} `{sc:+.2f}` [{it['title']}]({link}) · _{it['source']}_")


if go:
    names = [x.strip() for x in assets.split(",") if x.strip()]
    if not names:
        st.warning("Enter at least one asset.")
        st.stop()
    http = uma.Http()
    anim = st.empty()
    anim.markdown(ANIM, unsafe_allow_html=True)
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
