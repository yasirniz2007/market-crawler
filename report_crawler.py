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
            cur, pri = eps_fmt(float(m.group("a"))), (eps_fmt(float(m.group("b"))) if m.group("b") else "")
            end = m.end("a")
        else:
            cur = money(float(m.group("a").replace(",", "")) * UNITS[m.group("ua").lower()])
            pri = (money(float(m.group("b").replace(",", "")) * UNITS[m.group("ub").lower()])
                   if m.group("b") else "")
            end = m.end("ua")
    except ValueError:
        return None
    a = line.rfind("$", 0, m.start("a"))
    return {"cur": cur, "prior": pri, "hl": (max(a, 0), end), "review": not pri,
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
    return {"cur": cur, "prior": pri, "hl": (toks[0]["a"], end), "review": review, "note": note}


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
    return {"tag": tag, "total": len(FIELDS) + 1, "lines": anim, "rows": rows, "seq": seq, "meta": meta}


# ---------------------------------------------------------------- animation
HTML = r"""<!doctype html><html><head><meta charset="utf-8"><style>
:root{color-scheme:dark}
body{margin:0;background:#0b1220;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#e5e7eb}
#view{position:relative;height:400px;overflow:hidden;background:#e5e7eb;border-radius:12px 12px 0 0}
#inner{position:absolute;left:0;right:0;top:0;padding:14px 18px 60px;transition:transform 1.3s ease-in-out}
.ln{font:12.5px/21px Georgia,'Times New Roman',serif;color:#111827;overflow-wrap:anywhere}
.w{display:inline-block;padding:0 1px;border-radius:2px}
.bx{outline:1.5px solid var(--c);background:color-mix(in srgb,var(--c) 16%,transparent)}
.val{border-radius:3px}
.val.on{outline:2px solid var(--c);background:color-mix(in srgb,var(--c) 22%,transparent);font-weight:700}
#legs{position:absolute;left:0;top:0;pointer-events:none;overflow:visible}
#spider{position:absolute;left:0;top:0;width:15px;height:32px;background:#070b17;border-radius:6px;
 box-shadow:0 0 14px #22d3ee,0 0 3px #22d3ee inset}
#spider:after{content:"";position:absolute;left:4px;bottom:4px;width:7px;height:7px;border-radius:50%;
 background:#22d3ee;box-shadow:0 0 9px #22d3ee}
#pill{position:absolute;left:50%;bottom:12px;transform:translateX(-50%);background:rgba(8,12,24,.92);
 border:1px solid #1f2a44;border-radius:999px;padding:9px 20px;font:600 15px ui-monospace,Menlo,Consolas,monospace;
 color:#7dd3fc;white-space:nowrap;box-shadow:0 4px 20px rgba(0,0,0,.35)}
#pill b{color:#22d3ee}
#panel{background:#05070d;border-radius:0 0 12px 12px;padding:12px 16px 14px;border-top:1px solid #111a2e}
#head{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px}
#head .t{font-weight:700;font-size:15px}#head .t span{color:#94a3b8;font-weight:500;font-size:12px}
button{background:#0f1a30;color:#7dd3fc;border:1px solid #1f2a44;border-radius:8px;padding:5px 10px;font-size:12px}
.sec{font:700 12px ui-monospace,Menlo,monospace;letter-spacing:.08em;color:#e5e7eb;margin:9px 0 3px;
 border-top:1px solid #111a2e;padding-top:7px}
.row{display:grid;grid-template-columns:1.25fr .85fr .85fr 1.3fr;gap:6px;font:12px/1.5 ui-monospace,Menlo,monospace;
 color:#64748b;padding:1px 0}
.row .lb{color:#94a3b8}.row .v1,.row .v2{text-align:right}
.row.done .lb{color:#e5e7eb}.row.done .v1{color:#e5e7eb;font-weight:700}.row.done .v2{color:#94a3b8}
.row.done.pop{animation:pop .9s ease-out}
.row .nt{font-size:10.5px;color:#475569;text-align:right}.row.rev .nt{color:#f59e0b}
.row.miss .v1{color:#475569}
@keyframes pop{from{background:rgba(34,211,238,.28)}to{background:transparent}}
.cols{display:grid;grid-template-columns:1.25fr .85fr .85fr 1.3fr;gap:6px;font:10.5px ui-monospace,monospace;color:#475569}
.cols span:nth-child(2),.cols span:nth-child(3),.cols span:nth-child(4){text-align:right}
</style></head><body>
<div id="view"><div id="inner"></div><div id="pill"></div></div>
<div id="panel"><div id="head"><div class="t"><span id="meta"></span></div><button id="rp">↻ Replay</button></div>
<div class="cols"><span>FIGURE</span><span>LATEST COL</span><span>PRIOR COL</span><span>SOURCE</span></div>
<div id="rows"></div></div>
<script>
const D=__DATA__;
const COLORS=['#eab308','#06b6d4','#d946ef','#22c55e','#f97316','#6366f1','#ef4444'];
const view=document.getElementById('view'),inner=document.getElementById('inner'),pill=document.getElementById('pill');
const rowsEl=document.getElementById('rows');
document.getElementById('meta').textContent=D.meta;
const esc=s=>s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const words=s=>s.split(' ').filter(x=>x.length).map(w=>'<span class="w">'+esc(w)+'</span>').join(' ');
D.lines.forEach((ln,i)=>{
  const d=document.createElement('div');d.className='ln';
  if(ln.h){d.innerHTML=words(ln.t.slice(0,ln.h[0]))+' <span class="val" id="v'+i+'">'+esc(ln.t.slice(ln.h[0],ln.h[1]))+'</span> '+words(ln.t.slice(ln.h[1]));}
  else d.innerHTML=words(ln.t);
  inner.appendChild(d);
});
const NS='http://www.w3.org/2000/svg';
const legs=document.createElementNS(NS,'svg');legs.id='legs';inner.appendChild(legs);
const spider=document.createElement('div');spider.id='spider';inner.appendChild(spider);
let sx=60,sy=26,tx=60,ty=26,run=0;
const NL=10;
function frame(t){
  sx+=(tx-sx)*0.06;sy+=(ty-sy)*0.06;
  spider.style.transform='translate('+(sx-7)+'px,'+(sy-16)+'px)';
  const vx=(tx-sx)*0.5,vy=(ty-sy)*0.5;let h='';
  for(let k=0;k<NL;k++){
    const a=k*(6.2832/NL)+Math.sin(t*0.0006+k)*0.35;
    const L=62+Math.abs(Math.sin(t*0.0021+k*1.7))*70;
    const ex=sx+Math.cos(a)*L-vx,ey=sy+Math.sin(a)*L-vy;
    const c=k%3===0?'#f43f5e':'#22d3ee';
    h+='<line x1="'+sx+'" y1="'+sy+'" x2="'+ex+'" y2="'+ey+'" stroke="'+c+'" stroke-width="2" stroke-linecap="round" opacity=".9"/>'+
       '<circle cx="'+ex+'" cy="'+ey+'" r="3" fill="'+c+'"/>';
  }
  legs.innerHTML=h;
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
function sizeLegs(){legs.setAttribute('width',inner.scrollWidth);legs.setAttribute('height',inner.scrollHeight);}
sizeLegs();addEventListener('resize',sizeLegs);
const allW=[...inner.querySelectorAll('.w')];
setInterval(()=>{
  const ir=inner.getBoundingClientRect(),near=[];
  for(const w of allW){const r=w.getBoundingClientRect();if(Math.abs(r.top-ir.top-sy)<70)near.push(w);}
  for(let n=0;n<3&&near.length;n++){
    const w=near[Math.floor(Math.random()*near.length)];
    w.style.setProperty('--c',COLORS[Math.floor(Math.random()*COLORS.length)]);w.classList.add('bx');
    setTimeout(()=>w.classList.remove('bx'),650);
  }
},230);
function build(){
  rowsEl.innerHTML='';let sec='';
  D.rows.forEach((r,i)=>{
    if(r.section!==sec){sec=r.section;const s=document.createElement('div');s.className='sec';s.textContent=sec;rowsEl.appendChild(s);}
    const d=document.createElement('div');d.className='row';d.id='r_'+r.key;
    d.innerHTML='<span class="lb">'+esc(r.label)+'</span><span class="v1">· · ·</span><span class="v2"></span><span class="nt"></span>';
    rowsEl.appendChild(d);
  });
}
const wait=ms=>new Promise(r=>setTimeout(r,ms));
function setPill(n,done){pill.innerHTML=esc(D.tag)+' &nbsp; <b>'+n+' of '+D.total+' figures</b>'+(done?' &#10003;':'');}
function relPos(el){const r=el.getBoundingClientRect(),i=inner.getBoundingClientRect();return[r.left-i.left+r.width/2,r.top-i.top+r.height/2];}
function scrollTo(y){
  const max=Math.max(0,inner.scrollHeight-view.clientHeight);
  inner.style.transform='translateY('+(-Math.min(max,Math.max(0,y-view.clientHeight*0.42)))+'px)';
}
async function go(){
  const id=++run;build();setPill(0,false);
  document.querySelectorAll('.val.on').forEach(v=>v.classList.remove('on'));
  inner.style.transform='translateY(0)';tx=60;ty=26;await wait(900);
  let n=0;
  for(let k=0;k<D.seq.length;k++){
    if(id!==run)return;
    const s=D.seq[k],v=document.getElementById('v'+s.line);if(!v)continue;
    const p=relPos(v);tx=p[0];ty=p[1];scrollTo(p[1]);
    await wait(1500);if(id!==run)return;
    v.style.setProperty('--c',COLORS[k%COLORS.length]);v.classList.add('on');
    const r=D.rows.find(x=>x.key===s.key),el=document.getElementById('r_'+s.key);
    el.querySelector('.v1').textContent=r.cur.length>26?r.cur.slice(0,26)+'…':r.cur;
    el.querySelector('.v2').textContent=r.prior;el.querySelector('.nt').textContent=r.note;
    el.classList.add('done','pop');if(r.note.includes('needs review'))el.classList.add('rev');
    n++;setPill(n,false);await wait(800);
  }
  D.rows.forEach(r=>{if(!r.found){const el=document.getElementById('r_'+r.key);el.classList.add('miss');
    el.querySelector('.v1').textContent='not found';el.querySelector('.nt').textContent='not in this document';}});
  tx=sx+8;ty=sy;setPill(n,true);
}
document.getElementById('rp').onclick=go;
go();
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
