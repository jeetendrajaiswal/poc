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
FULL_DEFS = os.path.join(ROOT, "taxonomy", "definitions.yaml")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Nifty-50 (display name -> pipeline key/slug). The 6 with sample reports reuse existing keys.
NIFTY50 = [
    ("Adani Enterprises", "adani"), ("Adani Ports & SEZ", "adaniports"),
    ("Apollo Hospitals", "apollohosp"), ("Asian Paints", "asianpaint"),
    ("Axis Bank", "axisbank"), ("Bajaj Auto", "bajajauto"),
    ("Bajaj Finance", "bajfinance"), ("Bajaj Finserv", "bajajfinsv"),
    ("Bharat Electronics", "bel"), ("Bharti Airtel", "bhartiartl"),
    ("Cipla", "cipla"), ("Coal India", "coalindia"),
    ("Dr Reddy's Laboratories", "reddy"), ("Eicher Motors", "eichermot"),
    ("Eternal (Zomato)", "eternal"), ("Grasim Industries", "grasim"),
    ("HCL Technologies", "hcltech"), ("HDFC Bank", "hdfcbank"),
    ("HDFC Life", "hdfclife"), ("Hero MotoCorp", "heromotoco"),
    ("Hindalco Industries", "hindalco"), ("Hindustan Unilever", "hindunilvr"),
    ("ICICI Bank", "icicibank"), ("IndusInd Bank", "indusindbk"),
    ("Infosys", "infosys"), ("ITC", "itc"),
    ("Jio Financial Services", "jiofin"), ("JSW Steel", "jswsteel"),
    ("Kotak Mahindra Bank", "kotakbank"), ("Larsen & Toubro", "lt"),
    ("Mahindra & Mahindra", "m_m"), ("Maruti Suzuki", "maruti"),
    ("Nestle India", "nestleind"), ("NTPC", "ntpc"),
    ("Oil & Natural Gas Corp", "ongc"), ("Power Grid Corp", "powergrid"),
    ("Reliance Industries", "reliance"), ("SBI Life Insurance", "sbilife"),
    ("Shriram Finance", "shriramfin"), ("State Bank of India", "sbin"),
    ("Sun Pharmaceutical", "sunpharma"), ("Tata Consultancy Services", "tcs"),
    ("Tata Consumer Products", "tataconsum"), ("Tata Motors", "tatamotors"),
    ("Tata Steel", "tatasteel"), ("Tech Mahindra", "techm"),
    ("Titan Company", "titan"), ("Trent", "trent"),
    ("UltraTech Cement", "ultracemco"), ("Wipro", "wipro"),
]
_KEYS = {k for _, k in NIFTY50}

jobs: dict = {}   # job_id -> {state, company, message}


def _run_job(job_id: str, key: str, display: str):
    jobs[job_id] = {"state": "running", "company": display,
                    "message": "Building structure map & extracting ~116 datapoints (model calls)…"}
    try:
        from src import phase0, export_wide
        phase0.run(key, defs_path=FULL_DEFS, out_suffix="_full", pdf_dir=UPLOAD_DIR)
        done_keys = sorted(f[:-len("_full.json")] for f in os.listdir(OUT_DIR)
                           if f.endswith("_full.json"))
        export_wide.main(done_keys)
        jobs[job_id] = {"state": "done", "company": display, "key": key,
                        "message": "Done — results stored in output/results_wide.xlsx"}
    except Exception as e:  # surface the failure to the UI
        jobs[job_id] = {"state": "error", "company": display, "message": f"{type(e).__name__}: {e}"}
    finally:
        # Privacy: delete the uploaded report as soon as processing ends — success OR error.
        # Only the extracted figures (results_wide.xlsx) are kept; the source document is not.
        try:
            os.remove(os.path.join(UPLOAD_DIR, f"{key}.pdf"))
        except OSError:
            pass


def _wide_rows():
    path = os.path.join(OUT_DIR, "results_wide.csv")
    if not os.path.exists(path):
        return [], []
    rows = list(csv.reader(open(path)))
    return rows[0], rows[1:]


app = Flask(__name__)

PAGE = """<!doctype html><html><head><meta charset=utf-8><title>Annual-Report Extractor</title>
<style>
 body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f6f7f9;color:#1a1a1a}
 .hd{background:#1F4E78;color:#fff;padding:18px 26px}.hd h1{margin:0;font-size:19px}
 .card{background:#fff;border:1px solid #e2e5e9;border-radius:10px;padding:20px;margin:18px 26px;max-width:760px}
 label{font-weight:600;font-size:13px;display:block;margin:12px 0 5px}
 select,input[type=file]{width:100%;padding:9px;border:1px solid #cfd4da;border-radius:7px;font-size:14px;box-sizing:border-box}
 button{margin-top:16px;background:#1F4E78;color:#fff;border:0;border-radius:7px;padding:11px 22px;font-size:14px;font-weight:600;cursor:pointer}
 button:disabled{opacity:.5;cursor:not-allowed}
 #status{margin-top:14px;font-size:14px;padding:10px 12px;border-radius:7px;display:none}
 .run{background:#fff7e6;border:1px solid #ffd591}.ok{background:#f6ffed;border:1px solid #b7eb8f}.err{background:#fff1f0;border:1px solid #ffa39e}
 .spin{display:inline-block;width:14px;height:14px;border:2px solid #ffd591;border-top-color:#fa8c16;border-radius:50%;animation:s .8s linear infinite;vertical-align:-2px;margin-right:7px}
 @keyframes s{to{transform:rotate(360deg)}}
 .wrap{overflow:auto;max-height:60vh;margin:0 26px 26px}
 table{border-collapse:separate;border-spacing:0;font-size:12px;white-space:nowrap;background:#fff}
 th,td{border:1px solid #e6e8eb;padding:5px 9px;text-align:right}
 thead th{position:sticky;top:0;background:#1F4E78;color:#fff;z-index:3;max-width:130px;white-space:normal;vertical-align:bottom}
 td.k,th.corner{position:sticky;left:0;text-align:left;min-width:210px;max-width:210px;white-space:normal}
 td.k{background:#fff;font-weight:700;z-index:2}
 th.corner{z-index:5}
 .nd{color:#b0b4ba;font-style:italic} a.dl{display:inline-block;margin:0 26px}
</style></head><body>
<div class=hd><h1>📄 Annual-Report Datapoint Extractor</h1>
<a href="/tables" style="color:#cfe3f5;font-size:13px">→ Raw table extraction</a></div>
<div class=card>
 <form id=f>
  <label>Company (Nifty 50)</label>
  <select name=company id=company required>
   <option value="" disabled selected>Select a company…</option>
   {% for name,key in companies %}<option value="{{key}}">{{name}}</option>{% endfor %}
  </select>
  <label>Annual report (PDF)</label>
  <input type=file name=pdf id=pdf accept="application/pdf" required>
  <button id=go type=submit>Process</button>
 </form>
 <div id=status></div>
</div>
<a class=dl href="/download" id=dl style="display:none">⬇ Download results_wide.xlsx</a>
<div class=wrap id=results></div>
<script>
const f=document.getElementById('f'),st=document.getElementById('status'),go=document.getElementById('go');
f.onsubmit=async e=>{e.preventDefault();
 const fd=new FormData(f); go.disabled=true;
 st.style.display='block'; st.className='run'; st.innerHTML='<span class=spin></span>Uploading…';
 let r=await fetch('/process',{method:'POST',body:fd}); let j=await r.json();
 if(j.error){st.className='err';st.textContent=j.error;go.disabled=false;return;}
 poll(j.job);
};
async function poll(job){
 let r=await fetch('/status/'+job); let j=await r.json();
 if(j.state==='running'){st.className='run';st.innerHTML='<span class=spin></span>'+j.message;setTimeout(()=>poll(job),3000);}
 else if(j.state==='done'){st.className='ok';st.textContent='✓ '+j.message;go.disabled=false;loadResults();}
 else{st.className='err';st.textContent='✗ '+j.message;go.disabled=false;}
}
async function loadResults(){
 let r=await fetch('/results'); let h=await r.text();
 document.getElementById('results').innerHTML=h;
 document.getElementById('dl').style.display='inline-block';
}
function tog(i){
 var d=document.getElementById('d'+i), e=document.getElementById('e'+i);
 var open=d.style.display==='none';
 d.style.display=open?'table-row':'none';
 e.textContent=open?'▾':'▸';
}
loadResults();
</script></body></html>"""


@app.route("/")
def index():
    return render_template_string(PAGE, companies=NIFTY50)


@app.route("/process", methods=["POST"])
def process():
    key = (request.form.get("company") or "").strip()
    pdf = request.files.get("pdf")
    if key not in _KEYS:
        return jsonify(error="Please select a valid company."), 400
    if not pdf or not pdf.filename.lower().endswith(".pdf"):
        return jsonify(error="Please upload a PDF file."), 400
    pdf.save(os.path.join(UPLOAD_DIR, f"{key}.pdf"))
    display = next(n for n, k in NIFTY50 if k == key)
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"state": "running", "company": display, "message": "Queued…"}
    threading.Thread(target=_run_job, args=(job_id, key, display), daemon=True).start()
    return jsonify(job=job_id)


@app.route("/status/<job_id>")
def status(job_id):
    return jsonify(jobs.get(job_id, {"state": "error", "message": "unknown job"}))


@app.route("/results")
def results():
    header, rows = _wide_rows()
    if not rows:
        return "<p style='margin:26px;color:#888'>No results yet.</p>"
    dps = header[3:]
    ncol = len(header)
    label = "<th class=corner>▸ Year · Company · Type</th>" + "".join(f"<th>{h}</th>" for h in dps)
    trs = []
    for i, r in enumerate(rows):
        kcell = (f'<td class=k onclick="tog({i})"><span class=exp id=e{i}>▸</span> '
                 f'{r[0]} · {r[1]} · {r[2]}</td>')
        tds = [kcell] + [f'<td class="{"nd" if v in ("Not disclosed", "N/A", "") else ""}">{v}</td>'
                         for v in r[3:]]
        trs.append("<tr>" + "".join(tds) + "</tr>")
        # expandable detail: this row's datapoints listed one below the other
        pairs = "".join(
            f'<div class=dp><span class=dpn>{dp}</span>'
            f'<span class="dpv {"nd" if v in ("Not disclosed", "N/A", "") else ""}">{v or "—"}</span></div>'
            for dp, v in zip(dps, r[3:]))
        trs.append(f'<tr class=detail id=d{i} style="display:none"><td colspan={ncol}>'
                   f'<div class=detbox><b>{r[0]} · {r[1]} · {r[2]}</b>{pairs}</div></td></tr>')
    return f"<table><thead><tr>{label}</tr></thead><tbody>{''.join(trs)}</tbody></table>"


@app.route("/download")
def download():
    path = os.path.join(OUT_DIR, "results_wide.xlsx")
    if not os.path.exists(path):
        return "no results yet", 404
    return send_file(path, as_attachment=True, download_name="results_wide.xlsx")


# --------------------------------------------------------------------------- raw tables page

TABLES_DIR = os.path.join(OUT_DIR, "tables")
os.makedirs(TABLES_DIR, exist_ok=True)

TABLES_PAGE = """<!doctype html><html><head><meta charset=utf-8><title>Raw Table Extraction</title>
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
 table{border-collapse:collapse;font-size:13px;background:#fff;margin:0 26px 26px}
 th,td{border:1px solid #e6e8eb;padding:6px 12px;text-align:left}
 thead th{background:#1F4E78;color:#fff}
 a.dl{color:#1F4E78;font-weight:600}
</style></head><body>
<div class=hd><h1>📑 Raw Table Extraction</h1>
<a href="/" style="color:#cfe3f5;font-size:13px">← Datapoint extractor</a></div>
<div class=card>
 <form id=f>
  <label>Company / report name (used for the output file)</label>
  <input type=text name=name id=name placeholder="e.g. WIPRO_FY2026" required pattern="[A-Za-z0-9_\\- ]+">
  <label>Annual report (PDF)</label>
  <input type=file name=pdf id=pdf accept="application/pdf" required>
  <label style="font-weight:400"><input type=checkbox name=fin_only checked style="width:auto">
   Financial statements section only — statements, notes &amp; schedules, standalone + consolidated (recommended)</label>
  <button id=go type=submit>Extract all tables</button>
 </form>
 <div id=status></div>
</div>
<table id=done><thead><tr><th>Workbook</th><th>Tables</th><th></th></tr></thead><tbody id=list></tbody></table>
<script>
const f=document.getElementById('f'),st=document.getElementById('status'),go=document.getElementById('go');
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
 else if(j.state==='done'){st.className='ok';st.textContent='✓ '+j.message;go.disabled=false;loadList();}
 else{st.className='err';st.textContent='✗ '+j.message;go.disabled=false;}
}
async function loadList(){
 let r=await fetch('/tables/list'); let js=await r.json();
 document.getElementById('list').innerHTML=js.map(x=>
  `<tr><td>${x.name}</td><td>${x.tables??''}</td><td><a class=dl href="/tables/download/${encodeURIComponent(x.name)}">⬇ download</a></td></tr>`).join('');
}
loadList();
</script></body></html>"""


def _run_tables_job(job_id: str, name: str, pdf_path: str, fin_only: bool):
    jobs[job_id] = {"state": "running", "company": name,
                    "message": "Extracting all tables (deterministic, no model calls)…"}
    try:
        from src.engine.tables import extract_to_excel
        out = os.path.join(TABLES_DIR, f"{name}_tables.xlsx")

        def progress(p, t):
            jobs[job_id]["message"] = f"Extracting tables… page {p}/{t}"

        n = extract_to_excel(pdf_path, out, progress=progress, financial_only=fin_only)
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
    fin_only = bool(request.form.get("fin_only"))
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"state": "running", "company": name, "message": "Queued…"}
    threading.Thread(target=_run_tables_job, args=(job_id, name, pdf_path, fin_only),
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


if __name__ == "__main__":
    # macOS AirPlay Receiver squats on :5000, so default to 8000 (override with PORT=...).
    port = int(os.getenv("PORT", "8005"))
    print(f" * Open  http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
