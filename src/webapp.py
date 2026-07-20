"""Minimal web UI for the extraction pipeline.

Upload an annual report, pick the company (Nifty-50 for now), click Process. The pipeline
runs in a background thread (it makes model calls, so it takes a few minutes), then the
wide-format results (output/results_wide.xlsx) are rebuilt and shown on the page.

  .venv/bin/python -m src.webapp           # http://127.0.0.1:5000
"""
from __future__ import annotations

import csv
import os
import re
import threading
import uuid

from flask import Flask, request, jsonify, send_file, render_template_string

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(ROOT)                                   # so relative output/ paths resolve
UPLOAD_DIR = os.path.join(ROOT, "uploads")
OUT_DIR = os.path.join(ROOT, "output")
os.makedirs(UPLOAD_DIR, exist_ok=True)

TABLES_DIR = os.path.join(OUT_DIR, "tables")
MDNA_DIR = os.path.join(OUT_DIR, "mdna")
os.makedirs(TABLES_DIR, exist_ok=True)
os.makedirs(MDNA_DIR, exist_ok=True)

jobs: dict = {}   # job_id -> {state, company, message}


app = Flask(__name__)

TABLES_PAGE = r"""<!doctype html><html><head><meta charset=utf-8><title>Financial Data Extraction</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
 :root{--brand:#1F4E78;--brand2:#2c6fa8;--ink:#1a2230;--muted:#5a6470;--line:#e2e5e9;--bg:#f4f6f9}
 *{box-sizing:border-box}
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:var(--bg);color:var(--ink)}
 .hd{background:linear-gradient(120deg,#1F4E78,#2c6fa8);color:#fff;padding:22px 30px;box-shadow:0 2px 10px rgba(31,78,120,.18)}
 .hd h1{margin:0;font-size:21px;letter-spacing:.2px}
 .hd p{margin:5px 0 0;font-size:13px;color:#d5e6f5}
 .wrap{max-width:820px;margin:22px auto;padding:0 22px}
 .card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:24px 26px;box-shadow:0 1px 3px rgba(16,24,40,.05)}
 label{font-weight:600;font-size:12.5px;display:block;margin:0 0 6px;color:#39424f}
 input,select{width:100%;padding:10px 11px;border:1px solid #cfd4da;border-radius:9px;font-size:14px;background:#fff;transition:border-color .15s,box-shadow .15s}
 input:focus,select:focus{outline:0;border-color:var(--brand2);box-shadow:0 0 0 3px rgba(44,111,168,.15)}
 .grid{display:grid;grid-template-columns:2fr 1fr 1fr;gap:14px}
 .field{margin-bottom:16px}
 .file-drop{border:1.5px dashed #c2cdd8;border-radius:11px;padding:16px;text-align:center;background:#fafcfe;cursor:pointer;transition:border-color .15s,background .15s}
 .file-drop:hover,.file-drop.hover{border-color:var(--brand2);background:#f0f7fd}
 .file-drop .fi{font-size:22px}
 .file-drop .ft{font-size:13.5px;color:var(--muted);margin-top:4px}
 .file-drop .fn{font-size:13.5px;color:var(--brand);font-weight:600;margin-top:4px;word-break:break-all}
 input[type=file]{display:none}
 button{background:linear-gradient(120deg,#1F4E78,#2c6fa8);color:#fff;border:0;border-radius:9px;padding:12px 26px;font-size:14.5px;font-weight:600;cursor:pointer;box-shadow:0 2px 8px rgba(31,78,120,.25)}
 button:hover{filter:brightness(1.06)}
 button:disabled{opacity:.5;cursor:not-allowed;filter:none}
 .actions{margin-top:20px;display:flex;align-items:center;gap:14px}
 .idpreview{font-size:12.5px;color:var(--muted)}
 .idpreview b{color:var(--brand);font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
 #status{margin-top:16px;font-size:14px;padding:11px 14px;border-radius:9px;display:none;line-height:1.5}
 .run{background:#fff7e6;border:1px solid #ffd591}.ok{background:#f6ffed;border:1px solid #b7eb8f}.err{background:#fff1f0;border:1px solid #ffa39e}.info{background:#e6f4ff;border:1px solid #91caff}
 .spin{display:inline-block;width:14px;height:14px;border:2px solid #ffd591;border-top-color:#fa8c16;border-radius:50%;animation:s .8s linear infinite;vertical-align:-2px;margin-right:7px}
 @keyframes s{to{transform:rotate(360deg)}}
 .tabs{display:flex;gap:4px;margin-bottom:20px;border-bottom:2px solid var(--line)}
 .tab{background:none;border:0;border-bottom:3px solid transparent;margin:0;padding:9px 16px;font-size:14px;font-weight:600;color:var(--muted);cursor:pointer;border-radius:0;box-shadow:none}
 .tab:hover{color:var(--brand)}
 .tab.active{color:var(--brand);border-bottom-color:var(--brand)}
 .mdlist{margin:16px 0 0;font-size:14px}
 .mdlist a{color:var(--brand);font-weight:600;margin-right:16px;cursor:pointer}
 .mdlist a:hover{text-decoration:underline}
 #mdoverlay{display:none;position:fixed;inset:0;background:rgba(20,30,45,.55);z-index:50}
 #mdmodal{position:fixed;top:5vh;left:50%;transform:translateX(-50%);width:min(920px,92vw);height:88vh;background:#fff;border-radius:12px;z-index:51;display:none;flex-direction:column;box-shadow:0 18px 60px rgba(0,0,0,.35)}
 #mdhead{display:flex;align-items:center;justify-content:space-between;padding:14px 24px;border-bottom:1px solid var(--line)}
 #mdhead h1{margin:0;font-size:17px;color:var(--brand)}
 #mdclose{background:none;border:0;font-size:26px;color:var(--muted);cursor:pointer;margin:0;padding:0 4px;line-height:1;box-shadow:none}
 #mdview{overflow-y:auto;padding:10px 30px 30px;font-size:14px;line-height:1.55;flex:1}
 #mdview h2{color:var(--brand);border-bottom:2px solid #e8edf3;padding-bottom:5px;margin-top:26px}
 #mdview h3{color:#28587f;margin-top:20px}
 #mdview h4{color:#3a6890;margin-top:16px}
 #mdview table{border-collapse:collapse;margin:10px 0}
 #mdview th,#mdview td{border:1px solid #dfe4e9;padding:5px 11px;text-align:right;font-size:13px}
 #mdview th{background:#f0f4f8;color:var(--brand)}
 #mdview td:first-child,#mdview th:first-child{text-align:left}
 #mdview li{margin:3px 0}
 a.dl{color:var(--brand);font-weight:600;text-decoration:none}
 a.dl:hover{text-decoration:underline}
 .sec{margin-top:22px}
 .sectitle{font-weight:700;font-size:14px;color:var(--brand);margin:0 0 10px;display:flex;align-items:center;gap:7px}
 .wbgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:11px}
 .wbcard{background:#fff;border:1px solid var(--line);border-radius:11px;padding:13px 15px;display:flex;flex-direction:column;gap:7px;transition:box-shadow .15s,transform .15s}
 .wbcard:hover{box-shadow:0 4px 14px rgba(16,24,40,.09);transform:translateY(-1px)}
 .wbname{font-weight:600;font-size:13px;word-break:break-all;color:var(--ink)}
 .wbmeta{display:flex;justify-content:space-between;align-items:center;font-size:12.5px;color:var(--muted)}
 .wbbadge{background:#eef3f8;color:var(--brand);border-radius:10px;padding:2px 10px;font-weight:600}
 .empty{color:var(--muted);font-size:13px;padding:6px 0}
</style></head><body>
<div class=hd><h1>📑 Financial Data Extraction</h1><p>Extract structured financials from quarterly filings & annual reports — generated once, reused every time.</p></div>
<div class=wrap>
<div class=card>
 <div class=tabs>
  <button type=button class="tab active" id=tab_qtr onclick="setMode('quarterly')">Quarterly filing</button>
  <button type=button class="tab" id=tab_mdna onclick="setMode('mdna')">MD&amp;A summary</button>
 </div>
 <form id=f>
  <input type=hidden name=mode id=mode value="quarterly">
  <div class=grid>
   <div class=field>
    <label>Company</label>
    <input type=text name=company id=company placeholder="e.g. Wipro" required oninput="updateId()">
   </div>
   <div class=field>
    <label>Fiscal year</label>
    <select name=fy id=fy onchange="updateId()">
     <option value=2025>2025</option>
     <option value=2026 selected>2026</option>
     <option value=2027 id=fy_2027>2027</option>
    </select>
   </div>
   <div class=field id=qtr_field>
    <label>Quarter</label>
    <select name=quarter id=quarter onchange="updateId()">
     <option value=Q1>Q1</option><option value=Q2>Q2</option>
     <option value=Q3>Q3</option><option value=Q4>Q4</option>
    </select>
   </div>
  </div>
  <div class=field>
   <label id=pdf_label>Quarterly results filing (PDF)</label>
   <div class=file-drop id=drop onclick="document.getElementById('pdf').click()">
    <div class=fi>📄</div>
    <div class=ft id=drop_txt>Click to choose a PDF, or drag &amp; drop here</div>
    <div class=fn id=drop_name></div>
   </div>
   <input type=file name=pdf id=pdf accept="application/pdf" required>
  </div>
  <div class=actions>
   <button id=go type=submit>Extract all tables</button>
   <span class=idpreview id=idprev></span>
  </div>
 </form>
 <div id=status></div>
</div>

<div class=sec id=wbwrap style="display:none">
 <div class=sectitle>📊 Generated workbooks</div>
 <div class=wbgrid id=list></div>
</div>
<div class=sec id=mdlist_wrap style="display:none">
 <div class=sectitle>📝 MD&amp;A summaries</div>
 <div class=mdlist><span id=mdlist></span></div>
</div>
</div>
<div id=mdoverlay onclick="closeMdna()"></div>
<div id=mdmodal>
 <div id=mdhead><h1 id=mdtitle></h1><button id=mdclose onclick="closeMdna()">&times;</button></div>
 <div id=mdview></div>
</div>
<script>
const f=document.getElementById('f'),st=document.getElementById('status'),go=document.getElementById('go');
const $=id=>document.getElementById(id);
function normCompany(s){return s.trim().replace(/[^A-Za-z0-9]+/g,'_').replace(/^_+|_+$/g,'').toUpperCase();}
function normFy(s){const d=(s.match(/\d+/g)||[]).join('');return d?'FY'+d:'';}
function canonName(){
 const m=$('mode').value, c=normCompany($('company').value), fy=normFy($('fy').value);
 if(!c||!fy)return '';
 return (m==='quarterly')?`${c}_${$('quarter').value}${fy}`:`${c}_${fy}`;
}
function updateId(){
 const n=canonName();
 $('idprev').innerHTML=n?`Output file: <b>${n}</b>`:'';
}
function setMode(m){
 $('mode').value=m;
 $('tab_qtr').className='tab'+(m==='quarterly'?' active':'');
 $('tab_mdna').className='tab'+(m==='mdna'?' active':'');
 $('qtr_field').style.display=(m==='quarterly')?'':'none';
 // FY 2027 is only a valid choice for quarterly filings
 const opt2027=$('fy_2027'); opt2027.hidden=(m!=='quarterly');
 if(m!=='quarterly'&&$('fy').value==='2027')$('fy').value='2026';
 $('pdf_label').textContent=(m==='quarterly')?'Quarterly results filing (PDF)':'Annual report (PDF)';
 $('go').textContent=(m==='mdna')?'Generate MD&A summary':'Extract all tables';
 updateId(); applyVis();
}
// file drop
const drop=$('drop'),pdf=$('pdf');
pdf.onchange=()=>{ $('drop_name').textContent=pdf.files[0]?pdf.files[0].name:''; $('drop_txt').textContent=pdf.files[0]?'Selected file:':'Click to choose a PDF, or drag & drop here'; };
;['dragenter','dragover'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add('hover');}));
;['dragleave','drop'].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove('hover');}));
drop.addEventListener('drop',e=>{ if(e.dataTransfer.files.length){pdf.files=e.dataTransfer.files;pdf.onchange();} });

let wbHas=false, mdHas=false;
function applyVis(){
 const m=$('mode').value;
 $('wbwrap').style.display=(m!=='mdna'&&wbHas)?'block':'none';
 $('mdlist_wrap').style.display=(m==='mdna'&&mdHas)?'block':'none';
}
f.onsubmit=async e=>{e.preventDefault();
 if(!pdf.files.length){st.style.display='block';st.className='err';st.textContent='Please choose a PDF file.';return;}
 const fd=new FormData(f); go.disabled=true;
 st.style.display='block'; st.className='run'; st.innerHTML='<span class=spin></span>Uploading…';
 let r=await fetch('/tables/process',{method:'POST',body:fd}); let j=await r.json();
 if(j.error){st.className='err';st.textContent=j.error;go.disabled=false;return;}
 if(j.cached){
  st.className='info';st.innerHTML='♻️ '+j.message;go.disabled=false;
  loadList();loadMdna();
  if(j.kind==='mdna'&&j.doc){viewMdna(j.doc);}
  return;
 }
 poll(j.job);
};
async function poll(job){
 let r=await fetch('/tables/status/'+job); let j=await r.json();
 if(j.state==='running'){st.className='run';st.innerHTML='<span class=spin></span>'+j.message;setTimeout(()=>poll(job),2000);}
 else if(j.state==='done'){st.className='ok';st.textContent='✓ '+j.message;go.disabled=false;loadList();loadMdna();if(j.kind==='mdna'&&j.doc){viewMdna(j.doc);}}
 else{st.className='err';st.textContent='✗ '+j.message;go.disabled=false;}
}
async function loadList(){
 let r=await fetch('/tables/list'); let js=await r.json();
 wbHas=js.length>0; applyVis();
 $('list').innerHTML=js.length?js.map(x=>
  `<div class=wbcard><div class=wbname>${x.name.replace('_tables.xlsx','')}</div>`+
  `<div class=wbmeta><span class=wbbadge>${x.tables??'?'} tables</span>`+
  `<a class=dl href="/tables/download/${encodeURIComponent(x.name)}">⬇ download</a></div></div>`).join('')
  :'<div class=empty>No workbooks yet.</div>';
}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function inline(s){return s.replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/(^|[^*])\*([^*]+)\*/g,'$1<i>$2</i>');}
function mdToHtml(md){
 const lines=md.split('\n'); let out=[],i=0,inList=0;
 const closeList=()=>{while(inList){out.push('</ul>');inList--;}};
 while(i<lines.length){
  let l=lines[i];
  if(l.startsWith('|')){ closeList();
   let rows=[]; while(i<lines.length&&lines[i].trim().startsWith('|')){rows.push(lines[i].trim());i++;}
   let html='<table>';
   rows.forEach((rw,ri)=>{
    const cells=rw.replace(/^\||\|$/g,'').split('|').map(c=>c.trim());
    if(cells.every(c=>/^[-: ]*$/.test(c)))return;
    const tag=(ri===0)?'th':'td';
    html+='<tr>'+cells.map(c=>`<${tag}>${inline(esc(c))}</${tag}>`).join('')+'</tr>';
   });
   out.push(html+'</table>'); continue;
  }
  let m;
  if(m=l.match(/^(#{1,6})\s+(.*)/)){ closeList(); out.push(`<h${m[1].length}>${inline(esc(m[2]))}</h${m[1].length}>`); }
  else if(/^\s*[-•]\s+/.test(l)){
   const depth=Math.floor((l.match(/^\s*/)[0].length)/2)+1;
   while(inList<depth){out.push('<ul>');inList++;}
   while(inList>depth){out.push('</ul>');inList--;}
   out.push('<li>'+inline(esc(l.replace(/^\s*[-•]\s+/,'')))+'</li>');
  }
  else if(/^---+$/.test(l.trim())){ closeList(); out.push('<hr>'); }
  else if(l.trim()===''){ closeList(); }
  else { closeList(); out.push('<p>'+inline(esc(l))+'</p>'); }
  i++;
 }
 closeList(); return out.join('\n');
}
async function loadMdna(){
 let r=await fetch('/mdna/list'); let js=await r.json();
 mdHas=js.length>0; applyVis();
 document.getElementById('mdlist').innerHTML=js.map(n=>`<a onclick="viewMdna('${n}')">${n.replace('_MDNA.md','')}</a>`).join('');
}
async function viewMdna(n){
 let r=await fetch('/mdna/view/'+encodeURIComponent(n)); let md=await r.text();
 document.getElementById('mdtitle').textContent=n.replace('_MDNA.md','')+' — MD&A Summary';
 document.getElementById('mdview').innerHTML=mdToHtml(md);
 document.getElementById('mdoverlay').style.display='block';
 document.getElementById('mdmodal').style.display='flex';
 document.getElementById('mdview').scrollTop=0;
 document.body.style.overflow='hidden';
}
function closeMdna(){
 document.getElementById('mdoverlay').style.display='none';
 document.getElementById('mdmodal').style.display='none';
 document.body.style.overflow='';
}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeMdna();});
updateId(); loadList(); loadMdna();
</script></body></html>"""


def _run_tables_job(job_id: str, name: str, pdf_path: str, fin_only: bool, vision: bool,
                    mode: str = "auto"):
    jobs[job_id] = {"state": "running", "company": name, "message": "Processing…"}
    try:
        if mode == "mdna":
            from src.engine.mdna import summarize_mdna
            md = summarize_mdna(pdf_path, log=lambda m: None)
            with open(os.path.join(MDNA_DIR, f"{name}_MDNA.md"), "w") as fh:
                fh.write(md)
            jobs[job_id] = {"state": "done", "company": name, "kind": "mdna",
                            "doc": f"{name}_MDNA.md",
                            "message": "Done — MD&A summary generated"}
            return
        from src.engine.tables import write_workbook
        from src.engine.tables_llm import extract_tables_smart
        out = os.path.join(TABLES_DIR, f"{name}_tables.xlsx")

        tables = extract_tables_smart(pdf_path, financial_only=fin_only,
                                      vision=vision, progress=None,
                                      log=lambda m: None, mode=mode)
        write_workbook(tables, out)
        n = len(tables)
        jobs[job_id] = {"state": "done", "company": name,
                        "message": f"Done — {n} tables extracted to {os.path.basename(out)}"}
    except Exception as e:
        jobs[job_id] = {"state": "error", "company": name, "message": f"{type(e).__name__}: {e}"}
    finally:
        # Privacy: the uploaded report is deleted as soon as processing ends.
        try:
            os.remove(pdf_path)
        except OSError:
            pass


@app.route("/")
@app.route("/tables")
def tables_page():
    return TABLES_PAGE


def _canonical_name(company: str, fy: str, quarter: str, mode: str) -> str:
    """Build a stable output identifier from the structured fields so the same
    company + period always maps to the same file (and can be de-duplicated)."""
    comp = re.sub(r"[^A-Za-z0-9]+", "_", company or "").strip("_").upper()
    digits = "".join(re.findall(r"\d+", fy or ""))
    fy_tag = f"FY{digits}" if digits else ""
    if not comp or not fy_tag:
        return ""
    if mode == "quarterly":
        q = (quarter or "").upper()
        if q not in ("Q1", "Q2", "Q3", "Q4"):
            return ""
        return f"{comp}_{q}{fy_tag}"
    return f"{comp}_{fy_tag}"          # annual / MD&A


@app.route("/tables/process", methods=["POST"])
def tables_process():
    mode = request.form.get("mode") or "quarterly"
    if mode not in ("annual", "quarterly", "mdna", "auto"):
        mode = "quarterly"

    name = _canonical_name(request.form.get("company", ""),
                           request.form.get("fy", ""),
                           request.form.get("quarter", ""),
                           mode)
    if not name:
        need_q = " and quarter" if mode == "quarterly" else ""
        return jsonify(error=f"Please provide a company, fiscal year{need_q}."), 400

    # De-duplication: if this exact company/period was already generated,
    # return the existing output instead of re-running the pipeline.
    if mode == "mdna":
        existing = os.path.join(MDNA_DIR, f"{name}_MDNA.md")
        if os.path.exists(existing):
            return jsonify(cached=True, kind="mdna", name=name,
                           doc=f"{name}_MDNA.md",
                           message=f"Already generated — showing existing MD&A summary for <b>{name}</b>.")
    else:
        existing = os.path.join(TABLES_DIR, f"{name}_tables.xlsx")
        if os.path.exists(existing):
            return jsonify(cached=True, kind="tables", name=name,
                           message=f"Already generated — existing workbook <b>{name}</b> is ready below.")

    pdf = request.files.get("pdf")
    if not pdf or not pdf.filename.lower().endswith(".pdf"):
        return jsonify(error="Please upload a PDF file."), 400
    pdf_path = os.path.join(UPLOAD_DIR, f"tables_{name}.pdf")
    pdf.save(pdf_path)
    fin_only = True   # financial-statements section only, always
    vision = True     # scanned pages are handled automatically in both modes
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"state": "running", "company": name, "message": "Queued…"}
    threading.Thread(target=_run_tables_job,
                     args=(job_id, name, pdf_path, fin_only, vision, mode),
                     daemon=True).start()
    return jsonify(job=job_id)


@app.route("/tables/status/<job_id>")
def tables_status(job_id):
    return jsonify(jobs.get(job_id, {"state": "error", "message": "unknown job"}))


@app.route("/tables/list")
def tables_list():
    out = []
    for fn in sorted(os.listdir(TABLES_DIR)):
        if not fn.endswith(".xlsx"):
            continue
        ntab = None
        try:
            from openpyxl import load_workbook
            wb = load_workbook(os.path.join(TABLES_DIR, fn), read_only=True)
            ntab = len(wb.sheetnames) - 1          # minus the Index sheet
            wb.close()
        except Exception:
            pass
        out.append({"name": fn, "tables": ntab})
    return jsonify(out)


@app.route("/tables/download/<path:name>")
def tables_download(name):
    name = os.path.basename(name)                  # no path traversal
    path = os.path.join(TABLES_DIR, name)
    if not name.endswith(".xlsx") or not os.path.exists(path):
        return "not found", 404
    return send_file(path, as_attachment=True, download_name=name)


@app.route("/mdna/list")
def mdna_list():
    out = [fn for fn in sorted(os.listdir(MDNA_DIR)) if fn.endswith(".md")]
    return jsonify(out)


@app.route("/mdna/view/<path:name>")
def mdna_view(name):
    name = os.path.basename(name)                  # no path traversal
    path = os.path.join(MDNA_DIR, name)
    if not name.endswith(".md") or not os.path.exists(path):
        return "not found", 404
    with open(path) as fh:
        return fh.read(), 200, {"Content-Type": "text/plain; charset=utf-8"}


if __name__ == "__main__":
    # macOS AirPlay Receiver squats on :5000, so default to 8000 (override with PORT=...).
    port = int(os.getenv("PORT", "8005"))
    print(f" * Open  http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
