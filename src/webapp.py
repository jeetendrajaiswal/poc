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

TABLES_PAGE = r"""<!doctype html><html><head><meta charset=utf-8><title>Raw Table Extraction</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f6f7f9;color:#1a1a1a}
 .hd{background:#1F4E78;color:#fff;padding:18px 26px}.hd h1{margin:0;font-size:19px}
 .card{background:#fff;border:1px solid #e2e5e9;border-radius:10px;padding:20px;margin:18px 26px;max-width:760px}
 label{font-weight:600;font-size:13px;display:block;margin:12px 0 5px}
 input[type=text],input[type=file]{width:100%;padding:9px;border:1px solid #cfd4da;border-radius:7px;font-size:14px;box-sizing:border-box}
 button{margin-top:16px;background:#1F4E78;color:#fff;border:0;border-radius:7px;padding:11px 22px;font-size:14px;font-weight:600;cursor:pointer}
 button:disabled{opacity:.5;cursor:not-allowed}
 #status{margin-top:14px;font-size:14px;padding:10px 12px;border-radius:7px;display:none}
 .run{background:#fff7e6;border:1px solid #ffd591}.ok{background:#f6ffed;border:1px solid #b7eb8f}.err{background:#fff1f0;border:1px solid #ffa39e}
 .spin{display:inline-block;width:14px;height:14px;border:2px solid #ffd591;border-top-color:#fa8c16;border-radius:50%;animation:s .8s linear infinite;vertical-align:-2px;margin-right:7px}
 @keyframes s{to{transform:rotate(360deg)}}
 .note{font-size:13px;color:#5a6470;margin-top:10px;line-height:1.5}
 .tabs{display:flex;gap:6px;margin-bottom:14px;border-bottom:2px solid #e2e5e9}
 .tab{background:none;border:0;border-bottom:3px solid transparent;margin:0;padding:8px 14px;font-size:14px;font-weight:600;color:#5a6470;cursor:pointer;border-radius:0}
 .tab.active{color:#1F4E78;border-bottom-color:#1F4E78}
 .mdlist{margin:0 26px 10px;font-size:14px}
 .mdlist a{color:#1F4E78;font-weight:600;margin-right:16px;cursor:pointer}
 #mdoverlay{display:none;position:fixed;inset:0;background:rgba(20,30,45,.55);z-index:50}
 #mdmodal{position:fixed;top:5vh;left:50%;transform:translateX(-50%);width:min(920px,92vw);height:88vh;background:#fff;border-radius:12px;z-index:51;display:none;flex-direction:column;box-shadow:0 18px 60px rgba(0,0,0,.35)}
 #mdhead{display:flex;align-items:center;justify-content:space-between;padding:14px 24px;border-bottom:1px solid #e2e5e9}
 #mdhead h1{margin:0;font-size:17px;color:#1F4E78}
 #mdclose{background:none;border:0;font-size:26px;color:#5a6470;cursor:pointer;margin:0;padding:0 4px;line-height:1}
 #mdview{overflow-y:auto;padding:10px 30px 30px;font-size:14px;line-height:1.55;flex:1}
 #mdview h2{color:#1F4E78;border-bottom:2px solid #e8edf3;padding-bottom:5px;margin-top:26px}
 #mdview h3{color:#28587f;margin-top:20px}
 #mdview h4{color:#3a6890;margin-top:16px}
 #mdview table{border-collapse:collapse;margin:10px 0}
 #mdview th,#mdview td{border:1px solid #dfe4e9;padding:5px 11px;text-align:right;font-size:13px}
 #mdview th{background:#f0f4f8;color:#1F4E78}
 #mdview td:first-child,#mdview th:first-child{text-align:left}
 #mdview li{margin:3px 0}
 a.dl{color:#1F4E78;font-weight:600}
 .wbwrap{margin:0 26px 18px;max-width:1100px}
 .wbtitle{font-weight:700;font-size:14px;color:#1F4E78;margin:0 0 8px}
 .wbgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:10px;max-height:44vh;overflow-y:auto;padding:4px 6px 4px 0}
 .wbcard{background:#fff;border:1px solid #e2e5e9;border-radius:9px;padding:11px 14px;display:flex;flex-direction:column;gap:5px}
 .wbname{font-weight:600;font-size:13px;word-break:break-all}
 .wbmeta{display:flex;justify-content:space-between;align-items:center;font-size:12.5px;color:#5a6470}
 .wbbadge{background:#eef3f8;color:#1F4E78;border-radius:10px;padding:1px 9px;font-weight:600}
</style></head><body>
<div class=hd><h1>📑 Raw Table Extraction</h1>
<a href="/" style="color:#cfe3f5;font-size:13px">← Datapoint extractor</a></div>
<div class=card>
 <div class=tabs>
  <button type=button class="tab active" id=tab_annual onclick="setMode('annual')">Annual report</button>
  <button type=button class="tab" id=tab_qtr onclick="setMode('quarterly')">Quarterly filing</button>
  <button type=button class="tab" id=tab_mdna onclick="setMode('mdna')">MD&amp;A summary</button>
 </div>
 <form id=f>
  <input type=hidden name=mode id=mode value="annual">
  <label>Company / report name (used for the output file)</label>
  <input type=text name=name id=name placeholder="e.g. WIPRO_FY2026" required pattern="[A-Za-z0-9_\\- ]+">
  <label id=pdf_label>Annual report (PDF)</label>
  <input type=file name=pdf id=pdf accept="application/pdf" required>

  <button id=go type=submit>Extract all tables</button>
 </form>
 <div id=status></div>
</div>
<div class=wbwrap id=wbwrap style="display:none"><div class=wbtitle>Workbooks</div><div class=wbgrid id=list></div></div>
<div class=mdlist id=mdlist_wrap style="display:none"><b>MD&amp;A summaries:</b> <span id=mdlist></span></div>
<div id=mdoverlay onclick="closeMdna()"></div>
<div id=mdmodal>
 <div id=mdhead><h1 id=mdtitle></h1><button id=mdclose onclick="closeMdna()">&times;</button></div>
 <div id=mdview></div>
</div>
<script>
const f=document.getElementById('f'),st=document.getElementById('status'),go=document.getElementById('go');
function setMode(m){
 document.getElementById('mode').value=m;
 document.getElementById('tab_annual').className='tab'+(m==='annual'?' active':'');
 document.getElementById('tab_qtr').className='tab'+(m==='quarterly'?' active':'');
 document.getElementById('tab_mdna').className='tab'+(m==='mdna'?' active':'');
 document.getElementById('pdf_label').textContent=(m==='annual')?'Annual report (PDF)':(m==='quarterly')?'Quarterly results filing (PDF)':'Annual report (PDF)';
 document.getElementById('name').placeholder=(m==='mdna')?'e.g. WIPRO_FY2026':(m==='annual')?'e.g. WIPRO_FY2026':'e.g. WIPRO_Q1FY27';
 document.getElementById('go').textContent=(m==='mdna')?'Generate MD&A summary':'Extract all tables';
 applyVis();
}
let wbHas=false, mdHas=false;
function applyVis(){
 const m=document.getElementById('mode').value;
 document.getElementById('wbwrap').style.display=(m!=='mdna'&&wbHas)?'block':'none';
 document.getElementById('mdlist_wrap').style.display=(m==='mdna'&&mdHas)?'block':'none';
}
f.onsubmit=async e=>{e.preventDefault();
 const fd=new FormData(f); go.disabled=true;
 st.style.display='block'; st.className='run'; st.innerHTML='<span class=spin></span>Uploading…';
 let r=await fetch('/tables/process',{method:'POST',body:fd}); let j=await r.json();
 if(j.error){st.className='err';st.textContent=j.error;go.disabled=false;return;}
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
 document.getElementById('list').innerHTML=js.map(x=>
  `<div class=wbcard><div class=wbname>${x.name.replace('_tables.xlsx','')}</div>`+
  `<div class=wbmeta><span class=wbbadge>${x.tables??'?'} tables</span>`+
  `<a class=dl href="/tables/download/${encodeURIComponent(x.name)}">⬇ download</a></div></div>`).join('');
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
loadList(); loadMdna();
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


@app.route("/tables/process", methods=["POST"])
def tables_process():
    name = re.sub(r"[^A-Za-z0-9_\- ]", "", (request.form.get("name") or "")).strip().replace(" ", "_")
    pdf = request.files.get("pdf")
    if not name:
        return jsonify(error="Please provide a company / report name."), 400
    if not pdf or not pdf.filename.lower().endswith(".pdf"):
        return jsonify(error="Please upload a PDF file."), 400
    pdf_path = os.path.join(UPLOAD_DIR, f"tables_{name}.pdf")
    pdf.save(pdf_path)
    fin_only = True   # financial-statements section only, always
    mode = request.form.get("mode") or "auto"
    if mode not in ("annual", "quarterly", "mdna", "auto"):
        mode = "auto"
    vision = True   # scanned pages are handled automatically in both modes
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
