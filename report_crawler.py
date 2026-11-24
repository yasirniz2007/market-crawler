"""
Report crawler: a spider crawls an SEC filing (earnings 8-K, 10-Q or 10-K),
pulls out key figures, and replays the crawl as an animation over the document.

Data source: SEC EDGAR (public). SEC asks every client to send a contact email
in the User-Agent header, so the app asks for yours.
Figures are found with pattern matching and flagged "needs review" when
ambiguous. Always verify against the source document. Not financial advice.
"""
import csv
import io
import json
import re
import time
from html.parser import HTMLParser

import requests
import streamlit as st
import streamlit.components.v1 as components

SEC = "https://www.sec.gov"
DATA = "https://data.sec.gov"


# ---------------------------------------------------------------- SEC access
def sec_get(url, ua, retries=3):
    last = None
    for i in range(retries):
        time.sleep(0.25)  # stay well under SEC's 10 requests/second limit
        try:
            r = requests.get(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}, timeout=30)
            if r.status_code in (403, 429):
                time.sleep(2 * (i + 1))
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"SEC request failed ({url}). {last or 'Blocked or rate limited, try again in a minute.'}")


@st.cache_data(ttl=86400, show_spinner=False)
def _tickers(ua):
    return sec_get(f"{SEC}/files/company_tickers.json", ua).json()


def find_company(ticker, ua):
    for row in _tickers(ua).values():
        if row["ticker"].upper() == ticker.upper():
            return int(row["cik_str"]), row["title"]
    raise RuntimeError(f"Ticker {ticker} not found on SEC EDGAR (US-listed companies only).")


def find_filing(cik, kind, ua):
    sub = sec_get(f"{DATA}/submissions/CIK{cik:010d}.json", ua).json()
    rec = sub["filings"]["recent"]
    for i, form in enumerate(rec["form"]):
        items = rec.get("items", [""] * len(rec["form"]))[i] or ""
        ok = (form == "8-K" and "2.02" in items) if kind == "8-K" else form == kind
        if ok:
            acc = rec["accessionNumber"][i]
            return {"form": form, "date": rec["filingDate"][i], "acc": acc,
                    "primary": rec["primaryDocument"][i], "name": sub.get("name", "")}
    raise RuntimeError(f"No recent {kind} found for this company.")


def filing_document_url(cik, f, ua):
    base = f"{SEC}/Archives/edgar/data/{cik}/{f['acc'].replace('-', '')}"
    if f["form"] != "8-K":
        return f"{base}/{f['primary']}"
    try:
        items = sec_get(f"{base}/index.json", ua).json()["directory"]["item"]
    except Exception:
        return f"{base}/{f['primary']}"
    names = [it["name"] for it in items if it["name"].lower().endswith((".htm", ".html"))]
    for pat in (r"ex[-_]?99[-_]?1", r"ex[-_]?99"):
        for n in names:
            if re.search(pat, n, re.I):
                return f"{base}/{n}"
    return f"{base}/{f['primary']}"


# ---------------------------------------------------------------- text
class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "section", "center"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, d):
        if not self.skip:
            self.out.append(d)


def html_to_lines(html):
    p = _Text()
    p.feed(html[:6_000_000])
    raw = "".join(p.out).replace("\xa0", " ")
    lines = []
    for ln in raw.split("\n"):
        ln = re.sub(r"\s+", " ", ln.replace("|", " ")).strip()
        if ln:
            lines.append(ln)
    return lines


# ---------------------------------------------------------------- extraction
TOK = re.compile(r"(\()?\s*\$?\s*(\d[\d,]*(?:\.\d+)?)\s*(\))?\s*(%)?")
SCALES = {"thousands": 1e3, "millions": 1e6, "billions": 1e9}
UNITS = {"billion": 1e9, "million": 1e6}

FIELDS = [  # key, label, section, label patterns, kind
    ("revenue", "Revenue", "INCOME STATEMENT",
     [r"total net revenues?", r"net revenues?", r"total revenues?", r"revenues?", r"total net sales", r"net sales"], "money"),
    ("net_income", "Net income", "INCOME STATEMENT",
     [r"net income applicable to common shareholders", r"net income attributable to [a-z .,&']+", r"net earnings", r"net income"], "money"),
    ("eps", "Diluted EPS", "INCOME STATEMENT",
     [r"diluted earnings per (?:common )?share", r"earnings per (?:common )?share[ -]*diluted", r"diluted eps", r"diluted"], "eps"),
    ("op_income", "Operating income", "INCOME STATEMENT",
     [r"income from operations", r"operating income", r"operating profit"], "money"),
    ("cash", "Cash & equivalents", "BALANCE SHEET", [r"cash and cash equivalents"], "money"),
    ("assets", "Total assets", "BALANCE SHEET", [r"total assets"], "money"),
    ("liabs", "Total liabilities", "BALANCE SHEET", [r"total liabilities(?! and)"], "money"),
    ("equity", "Shareholders' equity", "BALANCE SHEET",
     [r"total (?:share|stock)holders['’]? equity", r"total equity"], "money"),
    ("ocf", "Operating cash flow", "CASH FLOW",
     [r"net cash (?:provided by|from|\(used (?:for|in)\) provided by|provided by \(used (?:for|in)\)) operating activities",
      r"cash provided by operat\w+"], "money"),
]
NARR_LABELS = {
    "revenue": r"(?:total )?net revenues?|total revenues?|revenues?|net sales",
    "net_income": r"net (?:income|earnings)(?: applicable to common shareholders)?",
    "eps": r"diluted (?:earnings per (?:common )?share|eps)",
}
CMP = r"(?:compared (?:with|to)|versus|up from|down from|from|vs\.?)"


def detect_scale(text):
    m = re.search(r"in (millions|thousands|billions)", text[:400000], re.I)
    return SCALES[m.group(1).lower()] if m else None


def tokens(s, base=0):
    out = []
    for m in TOK.finditer(s):
        if m.group(4):
            continue
        txt = m.group(2)
        v = float(txt.replace(",", ""))
        plain = "," not in txt and "." not in txt
        if plain and 1990 <= v <= 2100:
            continue
        neg = bool(m.group(1) and m.group(3))
        if neg and plain and v < 10 and not out:
            continue  # footnote marker like (1)
        out.append({"v": -v if neg else v, "txt": txt, "a": base + m.start(2), "b": base + m.end(2)})
    return out


def money(v):
    a = abs(v)
    s = "-" if v < 0 else ""
    if a >= 1e9:
        return f"{s}${a / 1e9:,.1f}B"
    if a >= 1e6:
        return f"{s}${a / 1e6:,.0f}M"
    return f"{s}${a:,.0f}"


def eps_fmt(v):
    return f"{'-' if v < 0 else ''}${abs(v):.2f}"


def _narrative(line, key):
    lab = NARR_LABELS[key]
    if key == "eps":
        rx = (rf"(?:{lab})[^.$]{{0,60}}?\$\s?(?P<a>\d+\.\d+)"
              rf"(?:[^$]{{0,120}}?{CMP}[^$]{{0,40}}?\$\s?(?P<b>\d+\.\d+))?")
    else:
        rx = (rf"(?:{lab})[^.$]{{0,50}}?\$\s?(?P<a>[\d.,]+)\s*(?P<ua>billion|million)"
              rf"(?:[^$]{{0,120}}?{CMP}[^$]{{0,60}}?\$\s?(?P<b>[\d.,]+)\s*(?P<ub>billion|million))?")
    m = re.search(rx, line, re.I)
    if not m:
        return None
    try:
        if key == "eps":
            cv = float(m.group("a"))
            pv = float(m.group("b")) if m.group("b") else None
            cur, pri = eps_fmt(cv), (eps_fmt(pv) if pv is not None else "")
            end = m.end("a")
        else:
            cv = float(m.group("a").replace(",", "")) * UNITS[m.group("ua").lower()]
            pv = (float(m.group("b").replace(",", "")) * UNITS[m.group("ub").lower()]) if m.group("b") else None
            cur, pri = money(cv), (money(pv) if pv is not None else "")
            end = m.end("ua")
    except ValueError:
        return None
    a = line.rfind("$", 0, m.start("a"))
    return {"cur": cur, "prior": pri, "cv": cv, "pv": pv, "hl": (max(a, 0), end), "review": not pri,
            "note": "" if pri else "no prior value in sentence"}


def _table_row(line, pat, kind, scale):
    m = re.match(rf"^(?:{pat})(?!\s+per\b)(?P<gap>[^\d$()%]{{0,60}})(?P<rest>.*)$", line, re.I)
    if not m:
        return None
    toks = tokens(m.group("rest"), m.start("rest"))
    if kind == "eps":
        toks = [t for t in toks if "." in t["txt"]]
    if not toks:
        return None
    mult = 1.0 if kind == "eps" else (scale or 1.0)
    f = eps_fmt if kind == "eps" else money
    cur = f(toks[0]["v"] * mult)
    pri = f(toks[1]["v"] * mult) if len(toks) > 1 else ""
    note, review = "", False
    if kind != "eps" and scale is None:
        note, review = "units not stated", True
    elif len(toks) >= 3:
        note, review = "3+ columns, check header", True
    elif len(toks) == 1:
        note, review = "single value", True
    end = toks[1]["b"] if len(toks) > 1 else toks[0]["b"]
    pv = toks[1]["v"] * mult if len(toks) > 1 else None
    return {"cur": cur, "prior": pri, "cv": toks[0]["v"] * mult, "pv": pv, "hl": (toks[0]["a"], end),
            "review": review, "note": note}


def extract_figures(lines):
    text = "\n".join(lines)
    scale = detect_scale(text)
    offsets, pos = [], 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    figs = []
    for key, label, section, pats, kind in FIELDS:
        hit = None
        if key in NARR_LABELS:
            for i, ln in enumerate(lines):
                if len(ln) > 30:
                    r = _narrative(ln, key)
                    if r:
                        hit = (i, r)
                        break
        if not hit:
            for pat in pats:
                for i, ln in enumerate(lines):
                    r = _table_row(ln, pat, kind, scale)
                    if r:
                        hit = (i, r)
                        break
                if hit:
                    break
        if hit:
            i, r = hit
            figs.append({"key": key, "label": label, "section": section, "line": i,
                         "page": offsets[i] // 3200 + 1, **r})
    # guidance sentence
    for i, ln in enumerate(lines):
        low = ln.lower()
        m = re.search(r"\b(expects?|expected|outlook|guidance|anticipates?|forecasts?)\b", low)
        if (m and re.search(r"\$\s?\d|\d+(\.\d+)?\s?%", ln) and "forward-looking" not in low
                and "forward looking" not in low and len(ln) < 400):
            figs.append({"key": "guidance", "label": "Guidance", "section": "GUIDANCE", "line": i,
                         "page": offsets[i] // 3200 + 1, "cur": (ln[:80] + "…") if len(ln) > 80 else ln,
                         "prior": "", "hl": (m.start(), m.end()), "review": True, "note": "verify wording"})
            break
    return figs, scale


def _window(line, a, b, width=98):
    if len(line) <= width:
        return line, (a, b)
    start = max(0, a - 38)
    seg = line[start:start + width]
    pre = "…" if start > 0 else ""
    return pre + seg, (a - start + len(pre), min(b - start + len(pre), len(pre) + len(seg)))


def build_payload(lines, figs, tag, meta):
    keep = set(range(min(2, len(lines))))
    for f in figs:
        keep.add(f["line"])
        if f["line"] > 0:
            keep.add(f["line"] - 1)
    order = sorted(keep)[:44]
    anim, index = [], {}
    fig_by_line = {f["line"]: f for f in figs}
    for i in order:
        f = fig_by_line.get(i)
        if f:
            t, hl = _window(lines[i], *f["hl"])
        else:
            t, hl = lines[i][:98], None
        index[i] = len(anim)
        anim.append({"t": t, "h": list(hl) if hl else None})
    by_key = {f["key"]: f for f in figs}
    rows = []
    for key, label, section, _p, _k in FIELDS + [("guidance", "Guidance", "GUIDANCE", None, None)]:
        f = by_key.get(key)
        rows.append({"key": key, "label": label, "section": section,
                     "found": bool(f and f["line"] in index), "cur": f["cur"] if f else "",
                     "prior": f["prior"] if f else "",
                     "note": (f"≈p.{f['page']} · " + ("needs review" + (f": {f['note']}" if f["note"] else "")
                                                         if f["review"] else "ok")) if f else ""})
    order_figs = sorted([f for f in figs if f["line"] in index], key=lambda x: x["line"])
    seq = [{"line": index[f["line"]], "key": f["key"]} for f in order_figs]
    name = meta.split(" · ")[0].upper()
    fk = {f["key"]: f for f in figs}

    def chg(f):
        if f and f.get("cv") is not None and f.get("pv"):
            return (f["cv"] / f["pv"] - 1) * 100
        return None

    cards = []
    for key, label in (("revenue", "REVENUE"), ("op_income", "OPERATING INCOME"),
                       ("net_income", "NET INCOME"), ("cash", "CASH AND CASH EQUIVALENTS")):
        f = fk.get(key)
        if f:
            c = chg(f)
            cards.append({"label": label, "value": f["cur"],
                          "change": (f"Change vs prior column: {c:+.1f}%" if c is not None and not f["review"]
                                     else "Prior column not clearly matched"),
                          "src": f"Reported figure; change calculated from the matching columns · ≈p.{f['page']}"})
    calc, obs = [], []
    r_, o_, n_ = fk.get("revenue"), fk.get("op_income"), fk.get("net_income")
    if r_ and o_ and r_.get("cv") and o_.get("cv") is not None:
        m = o_["cv"] / r_["cv"] * 100
        calc.append({"label": "Operating margin", "text": f"{m:.1f}%"})
        obs.append(f"Operating income was {m:.1f}% of revenue.")
    if r_ and n_ and r_.get("cv") and n_.get("cv") is not None:
        m = n_["cv"] / r_["cv"] * 100
        calc.append({"label": "Net margin", "text": f"{m:.1f}%"})
        obs.append(f"Net income was {m:.1f}% of revenue.")
    c = chg(r_)
    if c is not None:
        calc.append({"label": "Revenue change vs prior column", "text": f"{c:+.1f}%"})
        obs.append(f"Revenue changed {c:+.1f}% versus the prior column.")
    return {"tag": tag, "name": name, "total": len(FIELDS) + 1, "lines": anim, "rows": rows, "seq": seq,
            "meta": meta, "cards": cards, "calc": calc, "obs": obs}


# ---------------------------------------------------------------- animation
HTML = r"""<!doctype html><html><head><meta charset="utf-8"><style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:#000;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#e5e7eb}
#app{position:relative}
#view{position:relative;height:410px;overflow:hidden;background:#000;border-radius:12px 12px 0 0}
#wm{position:absolute;left:0;right:0;top:12px;text-align:center;font:italic 800 32px Georgia,serif;color:#fff;z-index:3;pointer-events:none;text-shadow:0 2px 14px #000}
#inner{position:absolute;left:0;right:0;top:0;padding:64px 18px 90px;transition:transform 1.3s ease-in-out}
.ln{font:10.5px/17px ui-monospace,Menlo,Consolas,monospace;color:#7c8498;border-bottom:1px dotted #1c2230;overflow-wrap:anywhere}
.w{display:inline-block;padding:0 1px;border-radius:2px}
.bx{outline:1.5px solid var(--c);background:color-mix(in srgb,var(--c) 18%,transparent)}
.val.on{outline:2px solid #facc15;background:rgba(250,204,21,.22);color:#fff;font-weight:700;border-radius:3px}
.badge{position:absolute;z-index:4;background:#facc15;color:#1a1300;font:800 15px ui-monospace,monospace;padding:3px 10px;border-radius:9px;box-shadow:0 0 18px rgba(250,204,21,.7);animation:fl 2.2s ease-out forwards;pointer-events:none;white-space:nowrap}
@keyframes fl{0%{opacity:0;transform:translate(-50%,6px) scale(.8)}15%{opacity:1;transform:translate(-50%,-8px) scale(1)}80%{opacity:1}100%{opacity:0;transform:translate(-50%,-34px)}}
#lab{position:absolute;left:50%;top:42%;transform:translate(-50%,-50%) scale(.9);z-index:5;background:#facc15;color:#1a1300;font:800 21px system-ui;padding:11px 20px;border-radius:14px;box-shadow:0 0 30px rgba(250,204,21,.6);opacity:0;transition:.35s;max-width:86%;text-align:center;pointer-events:none}
#lab.on{opacity:1;transform:translate(-50%,-50%) scale(1)}
#legs{position:absolute;left:0;top:0;pointer-events:none;overflow:visible}
#spider{position:absolute;left:0;top:0;width:15px;height:30px;background:#0a0a0a;border:2px solid #facc15;border-radius:7px;box-shadow:0 0 14px #facc15}
#spider:after{content:"";position:absolute;left:3px;bottom:3px;width:7px;height:7px;border-radius:50%;background:#38bdf8;box-shadow:0 0 9px #38bdf8}
#pill{position:absolute;left:50%;bottom:12px;transform:translateX(-50%);z-index:6;background:rgba(0,0,0,.92);border:1px solid #facc15;border-radius:999px;padding:9px 22px;font:600 15px ui-monospace,Menlo,Consolas,monospace;color:#facc15;white-space:nowrap;box-shadow:0 0 18px rgba(250,204,21,.25)}
#panel{background:#07080c;border-radius:0 0 12px 12px;padding:12px 16px 14px;border-top:1px solid #151a26}
#head{display:flex;justify-content:space-between;align-items:center;gap:8px}
#head .t{font-weight:800;font-size:17px;letter-spacing:.01em}#head .t span{color:#94a3b8;font-weight:400;font-size:13px}
button{background:#14110a;color:#facc15;border:1px solid #3a2f0a;border-radius:8px;padding:5px 10px;font-size:12px}
#shown{font:12.5px ui-monospace,Menlo,monospace;color:#94a3b8;margin:6px 0 2px}
.sec{font:700 12px ui-monospace,Menlo,monospace;letter-spacing:.08em;color:#e5e7eb;margin:10px 0 3px;border-top:1px solid #151a26;padding-top:7px}
.sec small{color:#566078;font-weight:400;letter-spacing:0;margin-left:6px}
.row{display:grid;grid-template-columns:1.25fr .85fr .85fr 1.3fr;gap:6px;font:12px/1.5 ui-monospace,Menlo,monospace;color:#566078;padding:1px 0}
.row .lb{color:#94a3b8}.row .v1,.row .v2{text-align:right}
.row.done .lb{color:#e5e7eb}.row.done .v1{color:#facc15;font-weight:700}.row.done .v2{color:#94a3b8}
.row.pop{animation:pop .9s ease-out}@keyframes pop{from{background:rgba(250,204,21,.25)}to{background:transparent}}
.row .nt{font-size:10.5px;color:#475569;text-align:right}.row.rev .nt{color:#f59e0b}.row.miss .v1{color:#475569}
.cols{display:grid;grid-template-columns:1.25fr .85fr .85fr 1.3fr;gap:6px;font:10.5px ui-monospace,monospace;color:#475569}
.cols span:nth-child(n+2){text-align:right}
.ob{display:flex;gap:10px;align-items:flex-start;border-left:3px solid #7a5a05;background:#100d05;padding:6px 10px;margin:5px 0;font:600 13px/1.4 system-ui;animation:pop 1s ease-out}
.ob i{font:700 9px ui-monospace,monospace;letter-spacing:.1em;font-style:normal;background:#3a2a05;color:#facc15;border-radius:999px;padding:3px 8px;white-space:nowrap}
.cr{display:flex;justify-content:space-between;font:12.5px ui-monospace,monospace;padding:2px 0;color:#e5e7eb;animation:pop 1s ease-out}.cr b{color:#facc15}
#sum{position:absolute;inset:0;z-index:20;background:#05060a;padding:18px 16px;overflow:auto;opacity:0;pointer-events:none;transition:opacity .6s;border-radius:12px}
#sum.on{opacity:1;pointer-events:auto}
#sum h2{margin:0 0 4px;font:800 22px system-ui}#sum h2 span{color:#facc15}
#sum h3{margin:2px 0 14px;font:700 13px ui-monospace,monospace;letter-spacing:.12em;color:#facc15}
.card{border-left:4px solid #facc15;background:#0b0c11;border-radius:8px;padding:12px 14px;margin-bottom:10px}
.card i{display:inline-block;font:700 11px ui-monospace,monospace;letter-spacing:.1em;font-style:normal;background:#facc15;color:#1a1300;border-radius:999px;padding:3px 11px}
.card .v{font:800 26px ui-monospace,monospace;margin:8px 0 4px}.card .c{font-size:14px;color:#cbd5e1}.card .s{font:11px/1.5 ui-monospace,monospace;color:#566078;margin-top:3px}
</style></head><body><div id="app">
<div id="view"><div id="wm"></div><div id="lab"></div><div id="inner"></div><div id="pill"></div></div>
<div id="panel"><div id="head"><div class="t" id="ttl"></div><button id="rp">&#8635; Replay</button></div>
<div id="shown"></div><div class="cols"><span>FIGURE</span><span>LATEST COL</span><span>PRIOR COL</span><span>SOURCE</span></div>
<div id="rows"></div><div id="extra"></div></div>
<div id="sum"></div></div>
<script>
const D=__DATA__;
const COLORS=['#38bdf8','#facc15'];
const $=id=>document.getElementById(id),view=$('view'),inner=$('inner'),pill=$('pill'),rowsEl=$('rows'),extra=$('extra'),sum=$('sum'),lab=$('lab');
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
$('wm').textContent=D.name;$('ttl').innerHTML=esc(D.name)+' <span>'+esc(D.tag)+'</span>';
const words=s=>s.split(' ').filter(x=>x.length).map(w=>'<span class="w">'+esc(w)+'</span>').join(' ');
D.lines.forEach((ln,i)=>{const d=document.createElement('div');d.className='ln';
 d.innerHTML=ln.h?words(ln.t.slice(0,ln.h[0]))+' <span class="val" id="v'+i+'">'+esc(ln.t.slice(ln.h[0],ln.h[1]))+'</span> '+words(ln.t.slice(ln.h[1])):words(ln.t);inner.appendChild(d)});
const NS='http://www.w3.org/2000/svg',legs=document.createElementNS(NS,'svg');legs.id='legs';inner.appendChild(legs);
const spider=document.createElement('div');spider.id='spider';inner.appendChild(spider);
let sx=60,sy=40,tx=60,ty=40,run=0;const NL=9;
function frame(t){sx+=(tx-sx)*.06;sy+=(ty-sy)*.06;spider.style.transform='translate('+(sx-7)+'px,'+(sy-15)+'px)';
 const vx=(tx-sx)*.5,vy=(ty-sy)*.5;let h='';
 for(let k=0;k<NL;k++){const a=k*(6.2832/NL)+Math.sin(t*.0006+k)*.35,L=70+Math.abs(Math.sin(t*.002+k*1.7))*85;
  const ex=sx+Math.cos(a)*L-vx,ey=sy+Math.sin(a)*L-vy,mx=(sx+ex)/2+Math.cos(a+1.57)*14*Math.sin(t*.003+k),my=(sy+ey)/2+Math.sin(a+1.57)*14*Math.sin(t*.003+k);
  const c=k%4===0?'#38bdf8':'#facc15';
  h+='<path d="M'+sx+' '+sy+'L'+mx+' '+my+'L'+ex+' '+ey+'" fill="none" stroke="'+c+'" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"/><circle cx="'+mx+'" cy="'+my+'" r="2.6" fill="#38bdf8"/><circle cx="'+ex+'" cy="'+ey+'" r="3" fill="'+c+'"/>'}
 legs.innerHTML=h;requestAnimationFrame(frame)}
requestAnimationFrame(frame);
function sizeLegs(){legs.setAttribute('width',inner.scrollWidth);legs.setAttribute('height',inner.scrollHeight)}sizeLegs();addEventListener('resize',sizeLegs);
const allW=[...inner.querySelectorAll('.w')];
setInterval(()=>{const ir=inner.getBoundingClientRect(),near=[];for(const w of allW){const r=w.getBoundingClientRect();if(Math.abs(r.top-ir.top-sy)<80)near.push(w)}
 for(let n=0;n<4&&near.length;n++){const w=near[Math.floor(Math.random()*near.length)];w.style.setProperty('--c',COLORS[Math.floor(Math.random()*2)]);w.classList.add('bx');setTimeout(()=>w.classList.remove('bx'),650)}},200);
function build(){rowsEl.innerHTML='';extra.innerHTML='';let sec='';
 D.rows.forEach(r=>{if(r.section!==sec){sec=r.section;const s=document.createElement('div');s.className='sec';s.textContent=sec;rowsEl.appendChild(s)}
  const d=document.createElement('div');d.className='row';d.id='r_'+r.key;
  d.innerHTML='<span class="lb">'+esc(r.label)+'</span><span class="v1">· · ·</span><span class="v2"></span><span class="nt"></span>';rowsEl.appendChild(d)});
 extra.innerHTML='<div class="sec">CALCULATED <small>shown once every input has been read</small></div><div id="calc"></div><div class="sec">OBSERVATIONS <small>shown when their evidence has been read</small></div><div id="obs"></div>'}
const wait=ms=>new Promise(r=>setTimeout(r,ms));
function setPill(n,done){pill.innerHTML=esc(D.tag)+' &nbsp; '+n+' of '+D.total+' figures'+(done?' &#10003;':'');$('shown').textContent=n+' of '+D.total+' report figures shown'}
function relPos(el){const r=el.getBoundingClientRect(),i=inner.getBoundingClientRect();return[r.left-i.left+r.width/2,r.top-i.top+r.height/2]}
function scrollTo(y){const max=Math.max(0,inner.scrollHeight-view.clientHeight);inner.style.transform='translateY('+(-Math.min(max,Math.max(0,y-view.clientHeight*.45)))+'px)'}
function showSum(){sum.innerHTML='<h2><span>&#9650;</span> Crawler</h2><h3>QUARTERLY RESULTS AND LIQUIDITY &middot; '+esc(D.name)+'</h3>'+
 D.cards.map(c=>'<div class="card"><i>'+esc(c.label)+'</i><div class="v">'+esc(c.value)+'</div><div class="c">'+esc(c.change)+'</div><div class="s">'+esc(c.src)+'</div></div>').join('')+
 '<button id="bk">&#8617; Back to crawl</button>';sum.classList.add('on');$('bk').onclick=()=>sum.classList.remove('on')}
async function go(){const id=++run;build();setPill(0,false);sum.classList.remove('on');
 document.querySelectorAll('.val.on').forEach(v=>v.classList.remove('on'));document.querySelectorAll('.badge').forEach(b=>b.remove());
 inner.style.transform='translateY(0)';tx=60;ty=40;await wait(900);let n=0;
 for(let k=0;k<D.seq.length;k++){if(id!==run)return;const s=D.seq[k],v=$('v'+s.line);if(!v)continue;
  const p=relPos(v);tx=p[0];ty=p[1];scrollTo(p[1]);const r=D.rows.find(x=>x.key===s.key);
  lab.textContent=r.label;lab.classList.add('on');await wait(1300);if(id!==run)return;lab.classList.remove('on');
  v.classList.add('on');const b=document.createElement('div');b.className='badge';b.textContent=r.cur.length>22?r.cur.slice(0,22)+'…':r.cur;b.style.left=p[0]+'px';b.style.top=(p[1]-14)+'px';inner.appendChild(b);setTimeout(()=>b.remove(),2300);
  const el=$('r_'+s.key);el.querySelector('.v1').textContent=r.cur.length>26?r.cur.slice(0,26)+'…':r.cur;el.querySelector('.v2').textContent=r.prior;el.querySelector('.nt').textContent=r.note;
  el.classList.add('done','pop');if(r.note.includes('needs review'))el.classList.add('rev');n++;setPill(n,false);await wait(700)}
 D.rows.forEach(r=>{if(!r.found){const el=$('r_'+r.key);el.classList.add('miss');el.querySelector('.v1').textContent='not found';el.querySelector('.nt').textContent='not in this document'}});
 $('calc').innerHTML=D.calc.map(c=>'<div class="cr"><span>'+esc(c.label)+'</span><b>'+esc(c.text)+'</b></div>').join('')||'<div class="cr"><span>not enough inputs</span></div>';
 await wait(600);$('obs').innerHTML=D.obs.map(o=>'<div class="ob"><i>OBSERVATION</i><span>'+esc(o)+'</span></div>').join('');
 tx=sx+8;ty=sy;setPill(n,true);await wait(2500);if(id===run&&D.cards.length)showSum()}
$('rp').onclick=go;go();
</script></body></html>"""


def animation_html(payload):
    data = json.dumps(payload).replace("</", "<\\/")
    return HTML.replace("__DATA__", data)


# ---------------------------------------------------------------- Streamlit UI
def render():
    st.markdown("### 📄 Report crawler")
    st.caption("Sends the spider into an SEC filing, pulls out the key figures, and replays the crawl over "
               "the document. US-listed companies only.")
    c1, c2 = st.columns(2)
    ticker = c1.text_input("Ticker", "GS", key="rc_ticker").strip().upper()
    kind_label = c2.selectbox("Filing", ["Latest earnings 8-K", "Latest 10-Q", "Latest 10-K"], key="rc_kind")
    email = st.text_input("Your email (SEC asks for a contact in each request)", key="rc_email",
                          placeholder="you@example.com")
    go = st.button("🕷️ Crawl report", type="primary", use_container_width=True, key="rc_go")

    if go:
        if "@" not in email:
            st.warning("Enter your email first. SEC requires a contact in the request header.")
        elif not ticker:
            st.warning("Enter a ticker, for example GS, AAPL or MSFT.")
        else:
            ua = f"MarketCrawler {email.strip()}"
            kind = kind_label.split()[-1]
            try:
                with st.status("Crawling…", expanded=True) as status:
                    st.write(f"▸ Looking up {ticker} on SEC EDGAR")
                    cik, name = find_company(ticker, ua)
                    st.write(f"▸ Finding the latest {kind} for {name}")
                    f = find_filing(cik, kind, ua)
                    st.write(f"▸ Locating the document (filed {f['date']})")
                    url = filing_document_url(cik, f, ua)
                    st.write("▸ Downloading and reading the text")
                    html = sec_get(url, ua).text
                    lines = html_to_lines(html)
                    st.write(f"▸ Extracting key figures from {len(lines):,} lines")
                    figs, scale = extract_figures(lines)
                    status.update(label=f"Done: {len(figs)} figures found", state="complete")
                tag = f"{ticker} {f['form']}"
                meta = f"{name} · {f['form']} · filed {f['date']}"
                st.session_state["rc_report"] = {
                    "payload": build_payload(lines, figs, tag, meta), "figs": figs, "url": url,
                    "meta": meta, "scale": scale}
            except Exception as e:
                st.error(str(e))

    rep = st.session_state.get("rc_report")
    if not rep:
        return
    st.caption(rep["meta"])
    components.html(animation_html(rep["payload"]), height=790, scrolling=False)
    if rep["scale"] is None:
        st.info("This document does not state its units, so money values are shown as printed and flagged.")
    rows = [{"Figure": f["label"], "Latest column": f["cur"], "Prior column": f["prior"],
             "≈Page": f["page"], "Check": ("needs review: " + f["note"]).strip(": ") if f["review"] else "ok"}
            for f in rep["figs"]]
    st.dataframe(rows, use_container_width=True, hide_index=True)
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
        st.download_button("⬇️ Download figures (CSV)", buf.getvalue(), "report_figures.csv", "text/csv")
    st.markdown(f"[Open the source document]({rep['url']})")
    st.caption("Figures are found with pattern matching, and columns are shown as printed. "
               "Always check against the source. Not financial advice.")
