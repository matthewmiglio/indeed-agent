"""Claude-in-the-loop profile ingestion GUI.

Boot:  python src/user_fill_info.py      (opens a local web UI)

Profiles tab: a card per profile (name + last-updated + pencil to edit), the
active one highlighted "Currently Selected", and a "+" card for the new-profile
flow. Editing / creating runs the Claude loop:

  1. Script gathers the existing profile + the desired schema (DATUMS).
  2. Claude figures out what's missing and compiles yes/no / text / textarea
     questions for just the gaps (+ a few narrative questions if history is thin).
  3. You answer in the GUI and submit.
  4. Claude files the answers: structured -> profiles/<name>/profile.yaml,
     narrative -> profiles/<name>/history/<title>.md.

Profiles live under user_data/profiles/<name>/; the active one is recorded in
user_data/active_profile (see profile_manager).
"""

import datetime
import json
import os
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(__file__))
from claude_client import chat_json  # noqa: E402
import profile_manager as pm  # noqa: E402

PORT = 8767  # profile GUI=8766, skills GUI=8765; keep distinct
MODEL = "claude-opus-4-8"  # interactive, low-volume; correctness of filing matters

GEN_QUESTIONS_SYSTEM = """You help ingest a job-applicant profile for an automated job-application agent.

You are given the DESIRED SCHEMA (fields with key/prompt/type/required/category) and the EXISTING PROFILE (keys already filled). Determine which desired fields are MISSING or empty, and write concise questions to collect ONLY those from the user. Never ask about a field that already has a value.

Also add up to 3 open narrative questions about the person's work experience ONLY if the existing narrative (experience_summary / work_history) looks thin — these enrich a resume corpus.

For each question pick a kind:
  - "yesno"    for boolean (type bool) fields,
  - "text"     for a short single-line value (name, city, salary, title, year...),
  - "textarea" for narrative / multi-sentence answers.
Map each structured question to its schema "key". Narrative questions use key null and kind "textarea".

Order questions by category. Keep prompts short and plain. If nothing is missing and the narrative is adequate, return an empty list.

Return ONLY valid JSON: {"questions": [{"id": "q1", "key": "city" | null, "kind": "text", "prompt": "...", "category": "..."}]}"""

FILE_ANSWERS_SYSTEM = """You file a user's answers into their profile for a job-application agent.

You are given the DESIRED SCHEMA, the EXISTING PROFILE, and QUESTIONS_AND_ANSWERS (each has key, kind, prompt, answer). Produce the updates.

Rules:
- For a structured key, output the value in the form the field expects: "yes"/"no" for bool, a number for int, a comma-separated string for list, plain text otherwise. Lightly normalize prose (e.g. "yeah I can" -> "yes"); do not invent facts the user didn't give.
- Skip any answer that is blank, "skip", or clearly a non-answer. Only include keys the user actually answered.
- For narrative answers (key null / kind textarea), compose them into one clean markdown document for the resume corpus and give it a short kebab-case title. If there were no narrative answers, set history_markdown and history_title to null.

Return ONLY valid JSON:
{"profile_updates": {"<schema_key>": "<value>"}, "history_markdown": "<string or null>", "history_title": "<kebab-title or null>"}"""


def _schema() -> list:
    return [{"key": k, "prompt": d["prompt"], "type": d["type"],
             "required": d["required"], "category": d["category"]}
            for k, d in pm.DATUMS.items()]


def _coerce(value, dtype: str):
    """Coerce a Claude-returned value to the field's Python type."""
    if isinstance(value, str):
        return pm._parse_value(value, dtype)
    if dtype == "list" and not isinstance(value, list):
        return [value] if value else []
    return value


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s[:60] or "notes"


def _updated(name: str) -> str:
    """Last-updated date of a profile's yaml as M/D/YY, or 'new'."""
    try:
        d = datetime.datetime.fromtimestamp(os.path.getmtime(pm.profile_path(name)))
        return f"{d.month}/{d.day}/{d.year % 100:02d}"
    except OSError:
        return "new"


def generate_questions(profile: dict) -> list:
    prompt = (f"DESIRED SCHEMA:\n{json.dumps(_schema(), indent=2)}\n\n"
              f"EXISTING PROFILE:\n{json.dumps(profile, indent=2, default=str)}\n\n"
              "Produce the questions JSON now.")
    raw, _ = chat_json(GEN_QUESTIONS_SYSTEM, prompt, model=MODEL, max_tokens=3000)
    return (json.loads(raw) or {}).get("questions", [])


def file_answers(profile: dict, qa: list) -> dict:
    prompt = (f"DESIRED SCHEMA:\n{json.dumps(_schema(), indent=2)}\n\n"
              f"EXISTING PROFILE:\n{json.dumps(profile, indent=2, default=str)}\n\n"
              f"QUESTIONS_AND_ANSWERS:\n{json.dumps(qa, indent=2)}\n\n"
              "Produce the updates JSON now.")
    raw, _ = chat_json(FILE_ANSWERS_SYSTEM, prompt, model=MODEL, max_tokens=4000)
    return json.loads(raw) or {}


def apply_updates(name: str, result: dict) -> dict:
    """Merge Claude's filed updates into profile.yaml + history/, return a summary."""
    profile = pm.load_profile(name)
    updates = result.get("profile_updates") or {}
    saved = []
    for key, val in updates.items():
        dtype = pm.DATUMS.get(key, {}).get("type", "str")
        coerced = _coerce(val, dtype)
        if coerced is None or coerced == "" or coerced == []:
            continue
        profile[key] = coerced
        saved.append(key)
    if saved:
        pm.save_profile(profile, name)

    history_file = None
    md = result.get("history_markdown")
    if md and md.strip():
        hdir = pm.history_dir(name)
        os.makedirs(hdir, exist_ok=True)
        history_file = _slug(result.get("history_title")) + ".md"
        with open(os.path.join(hdir, history_file), "w", encoding="utf-8") as f:
            f.write(md)
    return {"saved_keys": saved, "history_file": history_file}


# Search-profile CRUD lives in search_profiles (shared with apply_preflight).
from search_profiles import (  # noqa: E402
    list_searches, load_search, save_search, active_search, set_active_search,
    SEARCH_DIR, ACTIVE_SEARCH, _file as _search_file)


# ---------------------------------------------------------------- web UI

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Indeed Application Tool</title><style>
*{box-sizing:border-box}body{font:15px/1.5 system-ui,sans-serif;max-width:900px;margin:1.5rem auto;padding:0 1rem;background:#0f1115;color:#e6e6e6}
.titlebar{border:1px solid #333;border-radius:8px;padding:.7rem 1rem;font-weight:600;font-size:1.05rem;background:#161920}
.tabs{display:flex;gap:.4rem;border:1px solid #333;border-radius:8px;padding:.4rem;margin:.6rem 0 1.2rem;background:#161920}
.tab{padding:.35rem 1rem;border-radius:6px;cursor:pointer;color:#bcc}
.tab.active{outline:2px solid #e2c044;color:#fff}
.grid{display:flex;flex-wrap:wrap;gap:1rem}
.card{position:relative;width:200px;min-height:112px;border:1px solid #333;border-radius:8px;padding:.8rem;background:#161920;cursor:pointer}
.card:hover{border-color:#555}
.card.selected{outline:3px solid #e2c044;border-color:#e2c044}
.card .name{font-weight:600}.card .upd{color:#889;font-size:.85rem;margin-top:.2rem}
.card .sel{color:#5b9cff;font-size:.85rem;margin-top:.6rem}
.pencil{position:absolute;right:.5rem;bottom:.4rem;font-size:1.1rem;color:#aab;cursor:pointer;padding:.1rem .3rem;border-radius:4px}
.pencil:hover{background:#2a2f3a;color:#fff}
.card.add{display:flex;align-items:center;justify-content:center;font-size:2.4rem;color:#888;border-style:dashed}
label{display:block;margin:.3rem 0 .15rem;font-weight:600}
input[type=text],textarea{width:100%;padding:.5rem;background:#1a1d24;color:#e6e6e6;border:1px solid #333;border-radius:6px}
textarea{min-height:5rem}button{margin-top:.6rem;padding:.55rem 1.1rem;background:#3b82f6;color:#fff;border:0;border-radius:6px;cursor:pointer;font-size:.95rem}
button.ghost{background:#444}.q{margin:1rem 0;padding:.8rem;background:#161920;border:1px solid #262b36;border-radius:8px}
.cat{color:#8aa;font-size:.8rem;text-transform:uppercase;letter-spacing:.05em;margin-top:1.3rem}
.radios label{display:inline-block;margin-right:1rem;font-weight:400}.muted{color:#889}.hide{display:none}
#sub-done{padding:1rem;background:#13251a;border:1px solid #2a5;border-radius:8px}
.editbar{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin-bottom:.6rem}
.editbar input{width:auto;flex:1;min-width:160px}
</style></head><body>

<div class="titlebar">Indeed Application Tool</div>
<div class="tabs"><div class="tab active" data-tab="profiles">Profiles</div><div class="tab" data-tab="apply">Apply</div></div>

<section id="profilesTab">
  <div id="sub-list"><div class="grid" id="cards"></div></div>

  <div id="sub-new" class="hide">
    <label>New profile name</label>
    <input id="newname" type="text" placeholder="e.g. Jane Doe" autofocus>
    <div><button id="newgo">Continue</button> <button class="ghost" onclick="toList()">Cancel</button></div>
    <p class="muted" id="newmsg"></p>
  </div>

  <div id="sub-loading" class="hide"><p>Claude is reviewing the profile and finding gaps…</p></div>

  <form id="sub-questions" class="hide">
    <div class="editbar">
      <span>Editing <b id="editname"></b></span>
      <input id="rename" type="text" placeholder="rename to…">
      <button type="button" class="ghost" id="renamego">Rename</button>
      <button type="button" class="ghost" onclick="toList()">Back</button>
    </div>
    <p class="muted" id="qsummary"></p>
    <div id="qlist"></div>
    <button type="submit" id="submitbtn">Submit</button>
  </form>

  <div id="sub-done" class="hide"></div>
</section>

<section id="applyTab" class="hide">
  <div id="search-list">
    <div style="font-weight:600;margin-bottom:.5rem">Search profiles</div>
    <div class="grid" id="searchCards"></div>

    <div style="border:1px solid #333;border-radius:8px;padding:.8rem;background:#161920;margin-top:1.4rem;max-width:380px">
      <div style="font-weight:600">Stats</div>
      <div class="muted">Runtime: <span id="stRuntime">—</span></div>
      <div class="muted">Jobs Applied: <span id="stApplied">0</span></div>
    </div>
    <button id="startApply" style="background:#16a34a;font-size:1.15rem;padding:.7rem 1.6rem;margin-top:1rem">Start Applying</button>
    <p class="muted" id="applyMsg"></p>
  </div>

  <form id="search-edit" class="hide">
    <div style="font-weight:600;margin-bottom:.6rem">Search Profile Editor</div>
    <label>Search profile title</label><input id="s_title" type="text">
    <label>Desired salary</label><input id="s_salary" type="text" placeholder="e.g. 200000 or 200k">
    <label>Remote, hybrid, or in-person</label>
    <select id="s_remote" style="width:100%;padding:.5rem;background:#1a1d24;color:#e6e6e6;border:1px solid #333;border-radius:6px">
      <option value="remote">Remote</option><option value="hybrid">Hybrid</option><option value="onsite">In-person</option><option value="any">Any</option>
    </select>
    <label>If in-person, what location</label><input id="s_location" type="text" placeholder="City, ST (or list of metros)">
    <label>What roles? (up to 3, comma-separated)</label><input id="s_roles" type="text" placeholder="e.g. Software Engineer, Backend Engineer, SWE">
    <div><button type="submit" id="s_save">Save</button> <button type="button" class="ghost" onclick="toSearchList()">Cancel</button></div>
    <p class="muted" id="s_msg"></p>
  </form>
</section>

<script>
let EDIT_NAME=null, CREATING=false;
const $=s=>document.querySelector(s);
const subs=["list","new","loading","questions","done"];
const showSub=s=>subs.forEach(v=>$("#sub-"+v).classList.toggle("hide",v!==s));
function toList(){ loadProfiles(); showSub("list"); }

document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>{
  document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("active",x===t));
  $("#profilesTab").classList.toggle("hide",t.dataset.tab!=="profiles");
  $("#applyTab").classList.toggle("hide",t.dataset.tab!=="apply");
});

async function loadProfiles(){
  const j=await (await fetch("/profiles")).json();
  const g=$("#cards"); g.innerHTML="";
  j.profiles.forEach(p=>{
    const c=document.createElement("div"); c.className="card"+(p.name===j.active?" selected":"");
    c.innerHTML=`<div class="name"></div><div class="upd">Last updated: ${p.updated}</div>`+
                (p.name===j.active?`<div class="sel">Currently Selected</div>`:"")+
                `<div class="pencil" title="Edit profile">&#9998;</div>`;
    c.querySelector(".name").textContent=p.name;
    c.onclick=()=>select(p.name);
    c.querySelector(".pencil").onclick=e=>{e.stopPropagation(); startEdit(p.name,false);};
    g.appendChild(c);
  });
  const add=document.createElement("div"); add.className="card add"; add.textContent="+";
  add.title="New profile"; add.onclick=()=>{CREATING=true; $("#newname").value=""; $("#newmsg").textContent=""; showSub("new"); $("#newname").focus();};
  g.appendChild(add);
}
loadProfiles();

async function select(name){ await fetch("/select",{method:"POST",body:JSON.stringify({name})}); loadProfiles(); }

$("#newgo").onclick=()=>{ const n=$("#newname").value.trim(); if(!n){$("#newmsg").textContent="Enter a name.";return;} startEdit(n,true); };

async function startEdit(name,create){
  EDIT_NAME=name; CREATING=create; showSub("loading");
  const r=await fetch("/start",{method:"POST",body:JSON.stringify({profile:name,create})});
  const j=await r.json();
  if(j.error){ alert(j.error); toList(); return; }
  EDIT_NAME=j.name; $("#editname").textContent=j.name; $("#rename").value=j.name;
  if(!j.questions.length){ $("#sub-done").innerHTML=`<b>Nothing missing for ${j.name}.</b> (${j.filled}/${j.total} required filled.)<br><button class="ghost" onclick="toList()">Back to profiles</button>`; showSub("done"); return; }
  renderQuestions(j);
}

$("#renamego").onclick=async()=>{
  const nw=$("#rename").value.trim(); if(!nw||nw===EDIT_NAME)return;
  const j=await (await fetch("/rename",{method:"POST",body:JSON.stringify({old:EDIT_NAME,new:nw})})).json();
  if(j.error){ alert(j.error); return; }
  EDIT_NAME=nw; $("#editname").textContent=nw;
}

function renderQuestions(j){
  $("#qsummary").textContent=`${j.filled}/${j.total} required filled. ${j.questions.length} question(s).`;
  const list=$("#qlist"); list.innerHTML=""; let lastCat=null;
  j.questions.forEach(q=>{
    if(q.category&&q.category!==lastCat){ const c=document.createElement("div"); c.className="cat"; c.textContent=q.category; list.appendChild(c); lastCat=q.category; }
    const d=document.createElement("div"); d.className="q"; d.dataset.id=q.id; d.dataset.key=q.key||""; d.dataset.kind=q.kind; d.dataset.prompt=q.prompt;
    let ctrl;
    if(q.kind==="yesno"){ ctrl=`<div class="radios"><label><input type="radio" name="${q.id}" value="yes">Yes</label><label><input type="radio" name="${q.id}" value="no">No</label><label><input type="radio" name="${q.id}" value="skip" checked>Skip</label></div>`; }
    else if(q.kind==="textarea"){ ctrl=`<textarea name="${q.id}"></textarea>`; }
    else { ctrl=`<input type="text" name="${q.id}">`; }
    d.innerHTML=`<label>${q.prompt}</label>${ctrl}`; list.appendChild(d);
  });
  showSub("questions");
}

$("#sub-questions").onsubmit=async e=>{
  e.preventDefault(); $("#submitbtn").disabled=true; $("#submitbtn").textContent="Filing…";
  const answers=[...document.querySelectorAll("#qlist .q")].map(d=>{
    const id=d.dataset.id; let answer="";
    if(d.dataset.kind==="yesno"){ const c=d.querySelector(`input[name="${id}"]:checked`); answer=c?c.value:"skip"; }
    else { const el=d.querySelector(`[name="${id}"]`); answer=el?el.value.trim():""; }
    return {id,key:d.dataset.key||null,kind:d.dataset.kind,prompt:d.dataset.prompt,answer};
  });
  const j=await (await fetch("/save",{method:"POST",body:JSON.stringify({name:EDIT_NAME,answers})})).json();
  $("#submitbtn").disabled=false; $("#submitbtn").textContent="Submit";
  if(j.error){ $("#sub-done").innerHTML="<b>Error:</b> "+j.error; }
  else{ $("#sub-done").innerHTML=`<b>Saved to ${j.name}.</b><br>Fields updated: ${j.saved_keys.length?j.saved_keys.join(", "):"none"}.`+(j.history_file?`<br>History written: history/${j.history_file}`:"")+`<br><button class="ghost" onclick="toList()">Back to profiles</button>`; }
  showSub("done");
};

// ---- Apply tab: search profiles ----
let EDIT_SEARCH=null;
function toSearchList(){ loadSearches(); $("#search-list").classList.remove("hide"); $("#search-edit").classList.add("hide"); }

async function loadSearches(){
  const j=await (await fetch("/searches")).json();
  const g=$("#searchCards"); g.innerHTML="";
  j.profiles.forEach(p=>{
    const c=document.createElement("div"); c.className="card"+(p.name===j.active?" selected":"");
    c.innerHTML=`<div class="name"></div><div class="upd">Updated: ${p.updated}</div>`+
                (p.name===j.active?`<div class="sel">Currently Selected</div>`:"")+
                `<div class="pencil" title="Edit search profile">&#9998;</div>`;
    c.querySelector(".name").textContent=p.title;
    c.onclick=()=>selectSearch(p.name);
    c.querySelector(".pencil").onclick=e=>{e.stopPropagation(); editSearch(p.name);};
    g.appendChild(c);
  });
  const add=document.createElement("div"); add.className="card add"; add.textContent="+";
  add.title="New search profile"; add.onclick=()=>editSearch(null);
  g.appendChild(add);
}

async function selectSearch(name){ await fetch("/search-select",{method:"POST",body:JSON.stringify({name})}); loadSearches(); }

async function editSearch(name){
  EDIT_SEARCH=name;
  let d={title:"",salary_min:"",remote_preference:"remote",location:"",roles:[]};
  if(name){ d=await (await fetch("/search-get?name="+encodeURIComponent(name))).json(); }
  $("#s_title").value=d.title||""; $("#s_salary").value=d.salary_min||"";
  $("#s_remote").value=d.remote_preference||"remote"; $("#s_location").value=d.location||"";
  $("#s_roles").value=(d.roles||[]).join(", "); $("#s_msg").textContent="";
  $("#search-list").classList.add("hide"); $("#search-edit").classList.remove("hide");
}

$("#search-edit").onsubmit=async e=>{
  e.preventDefault();
  const title=$("#s_title").value.trim();
  if(!title){ $("#s_msg").textContent="Title is required."; return; }
  const roles=$("#s_roles").value.split(",").map(s=>s.trim()).filter(Boolean).slice(0,3);
  const body={orig:EDIT_SEARCH,data:{title,salary_min:$("#s_salary").value.trim(),
    remote_preference:$("#s_remote").value,location:$("#s_location").value.trim(),roles}};
  const j=await (await fetch("/search-save",{method:"POST",body:JSON.stringify(body)})).json();
  if(j.error){ $("#s_msg").textContent=j.error; return; }
  toSearchList();
};

$("#startApply").onclick=()=>{
  $("#applyMsg").textContent="Live applying isn't wired to this button yet — it would submit real applications. Run it from the apply-iteration-loop skill / CLI for now.";
};

// load search profiles when the Apply tab is first shown
document.querySelector('.tab[data-tab="apply"]').addEventListener("click",loadSearches);
</script></body></html>"""


def _make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if self.path == "/":
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif self.path == "/profiles":
                profiles = [{"name": n, "updated": _updated(n)} for n in pm.list_profiles()]
                self._send(200, json.dumps({"profiles": profiles, "active": pm.active_profile()}))
            elif self.path == "/searches":
                self._send(200, json.dumps({"profiles": list_searches(), "active": active_search()}))
            elif self.path.startswith("/search-get"):
                from urllib.parse import urlparse, parse_qs
                name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
                self._send(200, json.dumps(load_search(name)))
            else:
                self._send(404, "{}")

        def do_POST(self):
            try:
                if self.path == "/select":
                    pm.set_active_profile(self._body().get("name", ""))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/start":
                    b = self._body()
                    name = (b.get("profile") or "").strip()
                    if not name:
                        return self._send(200, json.dumps({"error": "No profile name."}))
                    if b.get("create") and name in pm.list_profiles():
                        return self._send(200, json.dumps({"error": f"Profile '{name}' already exists."}))
                    os.makedirs(pm.profile_dir(name), exist_ok=True)
                    pm.set_active_profile(name)
                    profile = pm.load_profile(name)
                    filled, total, _ = pm.check_completeness(profile)
                    questions = generate_questions(profile)
                    self._send(200, json.dumps({"name": name, "questions": questions,
                                                "filled": filled, "total": total}))
                elif self.path == "/save":
                    b = self._body()
                    name = (b.get("name") or pm.active_profile()).strip()
                    answered = [a for a in b.get("answers", [])
                                if (a.get("answer") or "").strip() and a.get("answer") != "skip"]
                    result = file_answers(pm.load_profile(name), answered) if answered else {}
                    summary = apply_updates(name, result)
                    summary["name"] = name
                    self._send(200, json.dumps(summary))
                elif self.path == "/rename":
                    b = self._body()
                    pm.rename_profile(b.get("old", ""), b.get("new", ""))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/search-select":
                    set_active_search(self._body().get("name", ""))
                    self._send(200, json.dumps({"ok": True}))
                elif self.path == "/search-save":
                    b = self._body()
                    name = save_search(b.get("data", {}), b.get("orig"))
                    if not active_search():
                        set_active_search(name)
                    self._send(200, json.dumps({"name": name}))
                elif self.path == "/stop":
                    self._send(200, "{}")
                    threading.Thread(target=state["httpd"].shutdown, daemon=True).start()
                else:
                    self._send(404, "{}")
            except Exception as e:  # surface failures to the GUI instead of a blank 500
                self._send(200, json.dumps({"error": f"{type(e).__name__}: {e}"}))

    return Handler


def serve(port: int = PORT, open_browser: bool = True):
    state = {}
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(state))
    state["httpd"] = httpd
    url = f"http://127.0.0.1:{port}/"
    print(f"Indeed Application Tool: {url}  (Ctrl-C to quit)")
    if open_browser:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    print("stopped.")


def _selfcheck():
    """Offline checks for the pure logic (no Claude calls)."""
    assert _coerce("yes", "bool") is True
    assert _coerce("no", "bool") is False
    assert _coerce("5", "int") == 5
    assert _coerce("Python, AWS", "list") == ["Python", "AWS"]
    assert _coerce(True, "bool") is True
    assert _slug("My Work History!") == "my-work-history"
    assert _slug("") == "notes"
    import tempfile
    pm.PROFILES_DIR = tempfile.mkdtemp()
    out = apply_updates("tmp", {"profile_updates": {"city": "Austin", "years_experience": "6", "blank": ""},
                                "history_markdown": "# Notes\nDid X.", "history_title": "Work Notes"})
    assert out["saved_keys"] == ["city", "years_experience"], out
    assert out["history_file"] == "work-notes.md", out
    assert pm.load_profile("tmp")["years_experience"] == 6
    # search profiles round-trip + retitle drops old file
    import search_profiles as sp
    sp.SEARCH_DIR = tempfile.mkdtemp()
    sp.ACTIVE_SEARCH = os.path.join(tempfile.mkdtemp(), "active_search")
    n1 = save_search({"title": "200k SWE Oceanfront", "roles": ["SWE"]})
    assert n1 == "200k-swe-oceanfront" and load_search(n1)["roles"] == ["SWE"]
    n2 = save_search({"title": "Renamed Search"}, orig=n1)
    assert n2 == "renamed-search" and not os.path.exists(_search_file(n1)), "old file not dropped"
    assert [p["name"] for p in list_searches()] == ["renamed-search"]
    print("selfcheck OK")


if __name__ == "__main__":
    if "--check" in sys.argv:
        _selfcheck()
    else:
        serve()
