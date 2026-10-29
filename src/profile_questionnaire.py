"""Profile questionnaire GUI — a local web UI equivalent of `main.py profile`.

Renders every datum from profile_manager.DATUMS with an input type matched to
its `type` (text / number / Yes-No radio / comma list), grouped by category and
pre-filled from the current profile. Save writes data/user_profile.yaml via
profile_manager.save_profile, then the server stops itself (like the skills GUI).

Run:  python src/main.py profile-gui      (or: serve_profile())
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(__file__))
from profile_manager import DATUMS, load_profile, save_profile, _parse_value  # noqa: E402

PORT = 8766  # skills GUI uses 8765; keep them distinct so both can be open


# Managed by the skills questionnaire (skills builder), NOT this form. Rendering
# them here as comma text boxes shredded multi-word/comma skills on save.
SKIP_KEYS = {"skills", "non_skills"}


def _prefill(value, dtype):
    """Turn a stored profile value into the string/flag the form needs."""
    if value in (None, "", []):
        return ""
    if dtype == "bool":
        return "yes" if value else "no"
    if dtype == "list":
        return "\n".join(str(x) for x in value)  # one item per line (comma-safe)
    return str(value)


# Dynamic work-history block appended to the Experience section. The cards
# themselves are rendered client-side (buildCard in the page JS) from
# EXISTING_JOBS, so the card shape lives in exactly one place.
JOBS_SECTION = """
<div class="jobs-wrap">
  <h3>Work history (past & present jobs)</h3>
  <p class="hint">Add one card per job. Blank cards are ignored on save.</p>
  <div id="jobs"></div>
  <button type="button" id="add-job">+ Add another job</button>
</div>
"""


def _build_rows():
    """One HTML block per category, each datum an input matched to its type."""
    profile = load_profile()
    cats = {}
    for key, d in DATUMS.items():
        cats.setdefault(d["category"], []).append((key, d))

    blocks = []
    for cat, items in cats.items():
        rows = [f'<h2>{cat}</h2>']
        for key, d in items:
            if key in SKIP_KEYS:
                continue
            cur = _prefill(profile.get(key), d["type"])
            req = ' <span class="req">*</span>' if d["required"] else ""
            label = f'<label for="{key}">{d["prompt"]}{req}</label>'
            if d["type"] == "bool":
                y = " checked" if cur == "yes" else ""
                n = " checked" if cur == "no" else ""
                field = (f'<span class="opts">'
                         f'<label class="yes"><input type="radio" name="{key}" value="yes"{y}>Yes</label>'
                         f'<label class="no"><input type="radio" name="{key}" value="no"{n}>No</label>'
                         f'<label><input type="radio" name="{key}" value=""{"" if cur else " checked"}>Unset</label>'
                         f'</span>')
            elif d["type"] == "list":
                esc = cur.replace("&", "&amp;").replace("<", "&lt;")
                field = f'<textarea id="{key}" name="{key}" placeholder="one per line" rows="3">{esc}</textarea>'
            else:
                itype = "number" if d["type"] == "int" else "text"
                esc = cur.replace('"', "&quot;")
                field = f'<input type="{itype}" id="{key}" name="{key}" value="{esc}" placeholder="">'
            rows.append(f'<div class="row" data-key="{key}" data-type="{d["type"]}">{label}{field}</div>')
        block = "\n".join(rows)
        if cat == "Experience":
            block += JOBS_SECTION  # dynamic multi-job repeater lives under Experience
        blocks.append(block)
    return "\n".join(blocks)


PAGE_TMPL = """<!doctype html>
<meta charset="utf-8">
<title>Profile Questionnaire</title>
<style>
  body { font: 15px/1.5 system-ui, sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }
  h1 { font-size: 1.4rem; }
  h2 { font-size: 1.05rem; margin: 1.6rem 0 .4rem; border-bottom: 2px solid #ddd; padding-bottom: .2rem; }
  .row { display: grid; grid-template-columns: 1fr auto; gap: 1rem; align-items: center; padding: .3rem .2rem; border-bottom: 1px solid #f0f0f0; transition: background .15s; }
  .row.unfilled { background: #fff6d6; box-shadow: inset 3px 0 0 #e0a800; }
  label { }
  .req { color: #b00; }
  input[type=text], input[type=number] { width: 300px; max-width: 45vw; padding: .35rem .5rem; font-size: .95rem; }
  .row textarea { width: 300px; max-width: 45vw; padding: .35rem .5rem; font: inherit; resize: vertical; }
  .opts label { margin-left: .7rem; cursor: pointer; }
  .opts .no { color: #b00; } .opts .yes { color: #070; }
  #save { margin: 1.4rem 0; padding: .55rem 1.2rem; font-size: 1rem; background: #070; color: #fff; border: 0; border-radius: 4px; cursor: pointer; }
  #save:disabled { background: #999; }
  #status { color: #666; margin: .5rem 0; }
  pre { background: #f6f6f6; padding: 1rem; white-space: pre-wrap; }
  .jobs-wrap { margin: .8rem 0 .4rem; }
  .jobs-wrap h3 { font-size: .98rem; margin: .8rem 0 .2rem; }
  .hint { color: #888; font-size: .85rem; margin: .1rem 0 .5rem; }
  .job-card { border: 1px solid #ddd; border-radius: 6px; padding: .7rem; margin: .5rem 0; background: #fafafa; }
  .job-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: .4rem; }
  .job-card input, .job-card textarea { display: block; width: 100%; box-sizing: border-box; margin: .25rem 0; padding: .35rem .5rem; font: inherit; }
  .job-card textarea { min-height: 3em; resize: vertical; }
  .rm { background: #fff; border: 1px solid #b00; color: #b00; border-radius: 4px; padding: .1rem .5rem; cursor: pointer; font-size: .8rem; }
  #add-job { background: #eef; border: 1px solid #99c; border-radius: 4px; padding: .4rem .9rem; cursor: pointer; }
</style>
<h1>Profile questionnaire</h1>
<p id="status">Required fields are marked <span class="req">*</span>. Existing values are pre-filled. Blank = leave unset.</p>
__ROWS__
<button id="save">Save to profile</button>
<pre id="out" style="display:none"></pre>
<script>
const EXISTING_JOBS = __EXISTING_JOBS__;
const JOB_FIELDS = [
  ["job_title","Job title"], ["employer","Employer"], ["location","Location (City, State)"],
  ["start_date","Start date (MM/YYYY)"], ["end_date","End date (MM/YYYY or Present)"],
];
const esc = s => (s==null?"":String(s)).replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/</g,"&lt;");

function buildCard(job) {
  job = job || {};
  const card = document.createElement("div");
  card.className = "job-card";
  const inputs = JOB_FIELDS.map(([k,ph]) =>
    `<input class="f" data-k="${k}" placeholder="${ph}" value="${esc(job[k])}">`).join("");
  card.innerHTML = `<div class="job-head"><strong>Job</strong>`
    + `<button type="button" class="rm">remove</button></div>` + inputs
    + `<textarea class="f" data-k="summary" placeholder="Responsibilities / achievements (optional)">${esc(job.summary)}</textarea>`;
  card.querySelector(".rm").onclick = () => card.remove();
  return card;
}

const jobsEl = document.querySelector("#jobs");
(EXISTING_JOBS.length ? EXISTING_JOBS : [{}]).forEach(j => jobsEl.appendChild(buildCard(j)));
document.querySelector("#add-job").onclick = () => {
  const c = buildCard({});
  jobsEl.appendChild(c);
  c.querySelector("input").focus();
};

// Highlight datum rows that aren't filled yet; clear as the user fills them.
function isFilled(row) {
  const radios = row.querySelectorAll("input[type=radio]");
  if (radios.length) { const c = row.querySelector("input[type=radio]:checked"); return !!(c && c.value); }
  const el = row.querySelector("input[type=text],input[type=number],textarea");
  return !!(el && el.value.trim());
}
function markRow(row) { row.classList.toggle("unfilled", !isFilled(row)); }
document.querySelectorAll(".row").forEach(row => {
  markRow(row);
  row.addEventListener("input", () => markRow(row));
  row.addEventListener("change", () => markRow(row));
});

document.querySelector("#save").onclick = async () => {
  const answers = {};
  document.querySelectorAll(".row").forEach(row => {
    const key = row.dataset.key;
    const checked = row.querySelector("input[type=radio]:checked");
    if (checked) { answers[key] = checked.value; }
    else { const el = row.querySelector("input[type=text],input[type=number],textarea"); if (el) answers[key] = el.value; }
  });
  const work_history = [];
  document.querySelectorAll(".job-card").forEach(card => {
    const job = {}; let any = false;
    card.querySelectorAll(".f").forEach(el => { const v = el.value.trim(); if (v) { job[el.dataset.k] = v; any = true; } });
    if (any) work_history.push(job);
  });
  const b = document.querySelector("#save"); b.disabled = true;
  document.querySelector("#status").textContent = "Saving...";
  const r = await fetch("/save", {method:"POST", body: JSON.stringify({answers, work_history})});
  const j = await r.json();
  const out = document.querySelector("#out"); out.style.display = "block";
  out.textContent = `Saved ${j.set} field(s) and ${j.jobs} work-history job(s) to data/user_profile.yaml.\\n`
    + `Required complete: ${j.filled}/${j.total}.`
    + (j.missing.length ? `\\nStill missing: ${j.missing.join(", ")}` : " All required fields filled.");
  document.querySelector("#status").textContent = j.server_stopping
    ? "Saved. Server stopped - you can close this tab." : "Saved.";
};
</script>
"""


def _make_handler(shutdown_on_save: bool):
    class Handler(BaseHTTPRequestHandler):
        def _deferred_shutdown(self):
            time.sleep(0.5)
            self.server.shutdown()

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/":
                existing = load_profile().get("work_history") or []
                page = (PAGE_TMPL
                        .replace("__ROWS__", _build_rows())
                        .replace("__EXISTING_JOBS__", json.dumps(existing)))
                self._send(200, page, "text/html; charset=utf-8")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            try:
                if self.path != "/save":
                    return self._send(404, "not found", "text/plain")
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                answers = body.get("answers") or {}
                work_history = body.get("work_history") or []
                profile = load_profile()
                set_count = 0
                for key, raw in answers.items():
                    if key not in DATUMS or key in SKIP_KEYS:
                        continue
                    if DATUMS[key]["type"] == "list":
                        # Newline-separated (comma-safe): a skill like
                        # "AWS (Lambda, S3, RDS)" stays one item.
                        val = [ln.strip() for ln in str(raw).splitlines() if ln.strip()]
                        if not val:
                            continue
                    else:
                        val = _parse_value(str(raw), DATUMS[key]["type"])
                        if val is None:
                            continue  # blank input leaves the existing value untouched
                    profile[key] = val
                    set_count += 1
                # Work history: replace with the submitted set (each a dict of
                # non-empty fields). Empty submission clears it.
                profile["work_history"] = [j for j in work_history if isinstance(j, dict) and j]
                save_profile(profile)

                required = [k for k, v in DATUMS.items() if v["required"]]
                missing = [k for k in required if profile.get(k) in (None, "", [])]
                filled = len(required) - len(missing)
                print("\n" + "=" * 60)
                print(f"RESULT: profile saved | set {set_count} field(s) | "
                      f"{len(profile['work_history'])} job(s) | required {filled}/{len(required)}")
                if missing:
                    print(f"  still missing: {', '.join(missing)}")
                print("=" * 60)

                self._send(200, json.dumps({
                    "set": set_count, "jobs": len(profile["work_history"]),
                    "filled": filled, "total": len(required),
                    "missing": missing, "server_stopping": shutdown_on_save,
                }))
                if shutdown_on_save:
                    print("  [profile] Save received - shutting down server.")
                    threading.Thread(target=self._deferred_shutdown, daemon=True).start()
            except Exception as e:
                print(f"[error] {type(e).__name__}: {e}")
                self._send(502, f"{type(e).__name__}: {e}", "text/plain")

        def log_message(self, *a):
            pass

    return Handler


def serve_profile(port: int = PORT, shutdown_on_save: bool = True):
    """Start the profile questionnaire web UI. Stops itself after a Save."""
    print(f"Profile questionnaire -> http://localhost:{port}")
    print("Fill it out in your browser and click Save. The server stops automatically on Save.")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(shutdown_on_save))
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        print("Profile questionnaire server stopped.")


if __name__ == "__main__":
    serve_profile()
