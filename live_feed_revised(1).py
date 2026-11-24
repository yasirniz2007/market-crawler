"""
Live news + economic calendar tab.

Sources (all public feeds, no scraping of sites that forbid it):
  - Economic calendar: the free weekly JSON feed that mirrors the Forex Factory calendar
    (title, currency, impact, forecast, previous; the free feed has no "actual" values)
  - FXStreet RSS (news + analysis), BBC World, Al Jazeera, CNBC RSS
  - Google News RSS searches for Bloomberg / Reuters headlines, war and geopolitics,
    central banks, oil and gold (headlines and links only)
  - TradingView's free embeddable widgets (calendar with actual values)
Not included: X/Twitter (paid API), MT5 (desktop terminal), and direct scraping of
Bloomberg, TradingView or Forex Factory pages (their terms forbid it).
Headline scoring is a simple word list. Not financial advice.
"""
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests
import streamlit as st
import streamlit.components.v1 as components

import report_crawler as rc
import universal_market_analyzer as uma

CAL_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FEEDS = [("FXStreet", "https://www.fxstreet.com/rss/news"),
         ("FXStreet Analysis", "https://www.fxstreet.com/rss/analysis"),
         ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
         ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
         ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html")]
TOPICS = [("Bloomberg", "site:bloomberg.com markets OR economy OR currencies"),
          ("Reuters", "site:reuters.com markets OR economy OR currencies"),
          ("War & geopolitics", "war OR missile OR ceasefire OR sanctions OR conflict"),
          ("Central banks", "Fed OR ECB OR \"Bank of England\" OR \"Bank of Japan\" interest rate decision"),
          ("Oil & gold", "oil price OR gold price")]
CURS = ["USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD"]
CURX = {"USD": r"\b(usd|dollar|fed|fomc|powell|treasury|greenback)\b", "EUR": r"\b(eur|euro|ecb|lagarde|eurozone)\b",
        "GBP": r"\b(gbp|pound|sterling|boe|bailey)\b", "JPY": r"\b(jpy|yen|boj|ueda)\b",
        "CHF": r"\b(chf|franc|snb)\b", "AUD": r"\b(aud|aussie|rba)\b", "CAD": r"\b(cad|loonie|boc)\b",
        "NZD": r"\b(nzd|kiwi|rbnz)\b"}
CATS = [("WAR / GEOPOLITICS", r"\b(war|missile|strikes?|attack|invasion|troops|ceasefire|sanctions?|conflict|military|drone|nuclear|hostages?|iran|israel|gaza|ukraine|russia|taiwan)\b"),
        ("CENTRAL BANK", r"\b(fed|fomc|ecb|boe|boj|rba|rbnz|snb|boc|powell|lagarde|bailey|ueda|rate (cut|hike|decision)|interest rates?)\b"),
        ("DATA", r"\b(cpi|inflation|nfp|payrolls|jobs|unemployment|gdp|pmi|retail sales|ppi)\b"),
        ("ENERGY", r"\b(oil|crude|brent|wti|opec)\b"), ("GOLD", r"\b(gold|silver|haven)\b")]
RISK_OFF = r"\b(war|attack|missile|sanctions?|recession|crash|plunge|selloff|sell-off|tensions?|escalat\w*|crisis|default|tariffs?|shutdown|fears?|slump)\b"
RISK_ON = r"\b(ceasefire|peace|rally|rallies|record high|rebound|optimism|deal|easing|stimulus|growth|upgrade|surge)\b"
KEY = re.compile(r"(CPI|Non-Farm|NFP|Employment Change|Unemployment|Interest Rate|Rate Statement|FOMC|Press Conference|"
                 r"GDP|PMI|Retail Sales|PCE|PPI|Trade Balance|Cash Rate|Monetary Policy|Speaks)", re.I)
ICON = {"High": "🔴", "Medium": "🟠", "Low": "🟡", "Holiday": "⚪"}


# ---------------------------------------------------------------- data
@st.cache_data(ttl=600, show_spinner=False)
def fetch_calendar():
    r = requests.get(CAL_URL, headers={"User-Agent": uma.UA}, timeout=20)
    r.raise_for_status()
    out = []
    for e in r.json():
        try:
            d = datetime.fromisoformat(e["date"])
        except Exception:
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        out.append({"title": e.get("title", ""), "cur": e.get("country", ""), "when": d.astimezone(timezone.utc),
                    "impact": e.get("impact", "Low"), "forecast": e.get("forecast") or "",
                    "previous": e.get("previous") or ""})
    return sorted(out, key=lambda x: x["when"])


@st.cache_data(ttl=300, show_spinner=False)
def fetch_news(region="US"):
    http = uma.Http(delay=0.5)
    raw = []
    for name, url in FEEDS:
        for it in uma._rss(http, url, name, ""):
            it["label"] = name
            raw.append(it)
    for name, q in TOPICS:
        for it in uma._google(http, q, region, "en", name, 12):
            t = it["title"]
            if " - " in t:
                t, src = t.rsplit(" - ", 1)
                it["title"], it["source"] = t, src
            it["label"] = name
            raw.append(it)
    now, seen, out = datetime.now(timezone.utc), set(), []
    for it in raw:
        k = re.sub(r"\W+", "", it["title"].lower())[:70]
        if not k or k in seen:
            continue
        seen.add(k)
        try:
            ts = parsedate_to_datetime(it["published"]).astimezone(timezone.utc)
        except Exception:
            ts = None
        if ts and now - ts > timedelta(hours=48):
            continue
        it["ts"] = ts
        out.append(it)
    return out


def headline_fx_score(title, currency, categories):
    """Estimate currency tone without treating every positive-sounding word as bullish.

    Geopolitical risk is handled separately from ordinary currency-specific language:
    risk-off tends to support traditional havens (USD, JPY, CHF) and pressure
    AUD/NZD. This is a rough heuristic, not a trading signal.
    """
    low = title.lower()
    is_geopolitical = "WAR / GEOPOLITICS" in categories
    risk_off = bool(re.search(RISK_OFF, low))
    risk_on = bool(re.search(RISK_ON, low))

    if is_geopolitical and (risk_off or risk_on):
        if risk_off:
            return {"USD": 0.6, "JPY": 0.6, "CHF": 0.6,
                    "AUD": -0.6, "NZD": -0.6}.get(currency, 0.0)
        return {"USD": -0.4, "JPY": -0.4, "CHF": -0.4,
                "AUD": 0.4, "NZD": 0.4}.get(currency, 0.0)

    return uma.sentiment(title, True)


def analyse(items):
    now = datetime.now(timezone.utc)
    acc = {c: [0.0, 0.0] for c in CURS}
    off = on = 0.0
    for it in items:
        age = (now - it["ts"]).total_seconds() / 3600 if it.get("ts") else 12
        w = 0.5 ** (age / 12)
        low = it["title"].lower()
        it["cats"] = [n for n, rx in CATS if re.search(rx, low)]
        it["curs"] = [c for c, rx in CURX.items() if re.search(rx, low)]
        it["fx"] = uma.sentiment(it["title"], True)
        off_hits = len(re.findall(RISK_OFF, low))
        on_hits = len(re.findall(RISK_ON, low))
        off += w * off_hits
        on += w * on_hits
        for c in it["curs"]:
            score = headline_fx_score(it["title"], c, it["cats"])
            acc[c][0] += w * score
            acc[c][1] += w
    mood = (on - off) / (on + off + 3)
    strength = {c: (v[0] / v[1] if v[1] else 0.0) for c, v in acc.items()}
    return mood, strength


def mood_label(m):
    return "RISK-OFF" if m < -0.15 else "RISK-ON" if m > 0.15 else "MIXED"


def rel(dt, now):
    s = int((dt - now).total_seconds())
    a = abs(s)
    txt = f"{a // 3600}h {a % 3600 // 60}m" if a >= 3600 else f"{a // 60}m"
    return f"in {txt}" if s >= 0 else f"{txt} ago"


# ---------------------------------------------------------------- spider animation payload
def build_anim(events, items, mood, off):
    now = datetime.now(timezone.utc)
    lines, rows, seq, cards = [], [], [], []
    for i, e in enumerate(events[:8]):
        loc = (e["when"] + timedelta(hours=off)).strftime("%a %H:%M")
        pre = f"{loc} {e['cur']} {e['title'][:38]}   "
        seg = f"forecast {e['forecast'] or '-'}  previous {e['previous'] or '-'}"
        lines.append({"t": pre + seg + f"   {e['impact'].upper()}", "h": [len(pre), len(pre) + len(seg)]})
        rows.append({"key": f"e{i}", "label": f"{e['cur']} {e['title'][:24]}", "section": "UPCOMING EVENTS",
                     "found": True, "cur": e["forecast"] or "—", "prior": e["previous"] or "—",
                     "note": f"{rel(e['when'], now)} · {e['impact']}"})
        seq.append({"line": len(lines) - 1, "key": f"e{i}"})
        if i < 4:
            cards.append({"label": f"{e['cur']} · {e['title']}"[:34].upper(), "value": e["forecast"] or "n/a",
                          "change": f"Previous: {e['previous'] or 'n/a'}",
                          "src": f"{e['impact']} impact · {loc} · the free feed has no actual values"})
    k = 0
    for it in sorted(items, key=lambda x: x["ts"] or now, reverse=True):
        tags = it["cats"] + it["curs"]
        if not tags or k >= 6:
            continue
        rx = dict(CATS).get(it["cats"][0]) if it["cats"] else CURX[it["curs"][0]]
        pre = f"[{it.get('source', it.get('label', 'News'))[:14]}] "
        text = pre + it["title"]
        m = re.search(rx, text, re.I)
        if not m or m.end() > 96:
            continue
        lines.append({"t": text[:98], "h": [m.start(), m.end()]})
        sig = "bullish tone" if it["fx"] > .15 else "bearish tone" if it["fx"] < -.15 else "neutral"
        rows.append({"key": f"h{k}", "label": (it["cats"] or it["curs"])[0], "section": "HEADLINE ALERTS",
                     "found": True, "cur": it.get("source", it.get("label", "News"))[:16], "prior": "", "note": sig})
        seq.append({"line": len(lines) - 1, "key": f"h{k}"})
        k += 1
    hi = sum(1 for e in events if e["impact"] == "High" and 0 <= (e["when"] - now).total_seconds() < 86400)
    top = sorted(((len([1 for it in items if c in it["curs"]]), c) for c in CURS), reverse=True)[:2]
    return {"tag": "LIVE", "name": "LIVE FEED", "unit": "items", "total": len(rows), "lines": lines,
            "rows": rows, "seq": seq, "meta": now.strftime("%Y-%m-%d %H:%M UTC"), "cards": cards,
            "cols": ["ITEM", "FORECAST / SOURCE", "PREVIOUS", "WHEN / SIGNAL"],
            "calc": [{"label": "Risk mood", "text": mood_label(mood)},
                     {"label": "High-impact events in 24h", "text": str(hi)},
                     {"label": "Headlines scanned", "text": str(len(items))}],
            "obs": [f"Most mentioned currencies: {top[0][1]} ({top[0][0]}) and {top[1][1]} ({top[1][0]})."
                    if items else "No headlines loaded.",
                    f"News tone is {mood_label(mood).lower()} based on war, sanctions, rally and similar words."]}


TV_CAL = ('<div class="tradingview-widget-container"><div class="tradingview-widget-container__widget"></div>'
          '<script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-events.js" async>'
          '{"colorTheme":"dark","isTransparent":true,"width":"100%","height":"520","locale":"en",'
          '"importanceFilter":"0,1","countryFilter":"us,eu,gb,jp,au,ca,nz,ch"}</script></div>')
TV_TAPE = ('<div class="tradingview-widget-container"><div class="tradingview-widget-container__widget"></div>'
           '<script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-ticker-tape.js" async>'
           '{"symbols":[{"proName":"FX:EURUSD","title":"EUR/USD"},{"proName":"FX:GBPUSD","title":"GBP/USD"},'
           '{"proName":"FX:USDJPY","title":"USD/JPY"},{"proName":"OANDA:XAUUSD","title":"Gold"},'
           '{"proName":"TVC:USOIL","title":"US Oil"},{"proName":"BITSTAMP:BTCUSD","title":"Bitcoin"}],'
           '"colorTheme":"dark","isTransparent":true,"displayMode":"adaptive","locale":"en"}</script></div>')


# ---------------------------------------------------------------- UI
def render():
    import pandas as pd
    st.markdown("### 📰 Live news & calendar")
    st.caption("Public feeds only: economic calendar (Forex Factory data feed), FXStreet, BBC, Al Jazeera, CNBC, and "
               "Bloomberg/Reuters/war/central-bank headlines via Google News. X/Twitter and MT5 can't be read here. "
               "Not financial advice.")
    c1, c2 = st.columns([3, 1])
    imp = c1.multiselect("Impact", ["High", "Medium", "Low"], ["High", "Medium"], key="lf_imp")
    off = c2.number_input("Your UTC offset", -12.0, 14.0, 0.0, 0.5, key="lf_off")
    cur_f = st.multiselect("Currencies", CURS + ["CNY"], CURS, key="lf_cur")
    if st.button("🔄 Refresh feeds", use_container_width=True, key="lf_ref"):
        fetch_calendar.clear()
        fetch_news.clear()

    err = None
    try:
        events = fetch_calendar()
    except Exception as e:
        events, err = [], f"Calendar feed unavailable ({e}). It is rate limited, try again in a few minutes."
    with st.spinner("Reading news feeds…"):
        items = fetch_news()
    if err:
        st.warning(err)
    mood, strength = analyse(items)
    now = datetime.now(timezone.utc)
    shift = timedelta(hours=off)

    if st.button("🕷️ Crawl live feed (spider)", type="primary", use_container_width=True, key="lf_spider"):
        st.session_state["lf_anim"] = True
    if st.session_state.get("lf_anim"):
        up = [e for e in events if e["when"] >= now - timedelta(minutes=30) and e["impact"] in imp
              and e["cur"] in cur_f]
        if up or items:
            components.html(rc.animation_html(build_anim(up, items, mood, off)), height=790, scrolling=False)

    st.markdown("#### Risk mood from headlines")
    st.progress(min(1.0, max(0.0, (mood + 1) / 2)))
    st.caption(f"{mood_label(mood)} ({mood:+.2f}). Risk-off usually favors USD, JPY, CHF and gold, and pressures "
               "AUD and NZD. This is a rough word count, not a forecast.")

    st.markdown("#### Upcoming events")
    up = [e for e in events if e["when"] >= now and e["impact"] in imp and e["cur"] in cur_f]
    heat = {c: sum(1 for e in up if e["cur"] == c and e["impact"] == "High" and (e["when"] - now).days < 1)
            for c in CURS}
    cols = st.columns(4)
    for i, c in enumerate(CURS):
        cols[i % 4].metric(c, f"{heat[c]} high", "next 24h", delta_color="off")
    if up:
        df = pd.DataFrame([{"When": (e["when"] + shift).strftime("%a %H:%M"), "In": rel(e["when"], now),
                            "Cur": e["cur"], "Event": ("⭐ " if KEY.search(e["title"]) else "") + e["title"],
                            "Impact": ICON.get(e["impact"], "") + " " + e["impact"],
                            "Forecast": e["forecast"], "Previous": e["previous"]} for e in up[:60]])
        st.dataframe(df, hide_index=True, use_container_width=True)
    elif not err:
        st.info("No upcoming events match your filters this week.")
    past = [e for e in events if e["when"] < now and e["impact"] in imp and e["cur"] in cur_f]
    if past:
        with st.expander(f"Already released this week ({len(past)})"):
            st.caption("The free feed has no actual values. Open the TradingView calendar below for actuals.")
            st.dataframe(pd.DataFrame([{"When": (e["when"] + shift).strftime("%a %H:%M"), "Cur": e["cur"],
                                        "Event": e["title"], "Forecast": e["forecast"],
                                        "Previous": e["previous"]} for e in past[-40:]]),
                         hide_index=True, use_container_width=True)

    st.markdown("#### Headlines")
    cats = st.multiselect("Topics", [c for c, _ in CATS], key="lf_cats")
    shown = [it for it in sorted(items, key=lambda x: x["ts"] or now, reverse=True)
             if not cats or any(c in it["cats"] for c in cats)][:40]
    for it in shown:
        dot = "🟢" if it["fx"] > .15 else "🔴" if it["fx"] < -.15 else "⚪"
        tags = " ".join(f"`{t}`" for t in (it["cats"] + it["curs"])[:3])
        age = rel(it["ts"], now) if it["ts"] else ""
        st.markdown(f"{dot} [{it['title']}]({it.get('link') or ''}) · _{it.get('source', it.get('label', 'News'))}_ · {age} {tags}")
    if not shown:
        st.info("No headlines loaded. Tap Refresh, or try again in a minute.")

    st.markdown("#### Currency strength from headlines")
    st.bar_chart(pd.DataFrame({"headline tone": strength}), height=200)
    pairs = []
    for p in uma.ALL_PAIRS:
        pairs.append((strength[p[:3]] - strength[p[3:]], f"{p[:3]}/{p[3:]}"))
    pairs.sort(reverse=True)
    t1, t2 = st.columns(2)
    t1.markdown("**Headline tone favors**<br>" + "<br>".join(f"{n} {s:+.2f}" for s, n in pairs[:5]),
                unsafe_allow_html=True)
    t2.markdown("**Headline tone against**<br>" + "<br>".join(f"{n} {s:+.2f}" for s, n in pairs[-5:][::-1]),
                unsafe_allow_html=True)

    with st.expander("TradingView live widgets (calendar with actual values)"):
        components.html(TV_TAPE, height=90)
        components.html(TV_CAL, height=540, scrolling=True)
