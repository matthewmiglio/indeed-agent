"""Skills yes/no questionnaire — a small local web UI that builds the profile's
`skills` (allowlist) and `non_skills` (denylist) lists.

Flow: enter your field -> Claude generates ~100 relevant skills -> answer
Yes/No/Skip on each -> Save. Yes -> `skills`, No -> `non_skills`, Skip -> neither.

Two modes:
  serve(apply=True)  -> Save writes the real data/user_profile.yaml (setup use).
  serve(apply=False) -> Save is a DRY RUN: writes a proposed_changes.yaml artifact
                        and never touches the real profile (test-suite use).

The `non_skills` denylist is consumed by resume_tuner.py so generated resumes
never claim a skill the candidate has explicitly said they lack.
"""

import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import yaml

sys.path.insert(0, os.path.dirname(__file__))
from claude_client import chat_json          # noqa: E402
from profile_manager import load_profile, save_profile  # noqa: E402

PORT = 8765
GEN_MODEL = "claude-opus-4-8"

SKILLS_SYSTEM = """You generate a broad skills inventory for one or more professional fields/roles, for a yes/no self-assessment questionnaire.

Include a WIDE range: core hard skills, adjacent/secondary tools, certifications and licenses, frameworks/platforms/software/equipment, and soft/process skills. Breadth is the entire point: the user will click "No" on plausible-but-absent items, so include skills a strong candidate MIGHT have but many would not.

If MULTIPLE fields are given, draw skills from ALL of them, distributed roughly EVENLY across the fields (not just skills they share). Each field must be well represented; a skill relevant to only one field is fine. Do not collapse everything into a generic overlap.

Rules:
- Return ONLY valid JSON, exactly: {"skills": ["...", "..."]}
- About 100 entries total (95-105 is fine), regardless of how many fields.
- Each entry is a short noun phrase (2-5 words), specific enough to answer yes/no.
- No duplicates or near-duplicates. No numbering, no prose, no markdown fences."""


def _split_fields(text: str) -> list[str]:
    """Split a free-text field string into distinct roles.

    Handles "dog groomer, vet, and bartender & nurse" -> 4 roles.
    """
    parts = re.split(r"[,;/]|\band\b|&", text, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


def _dedup(items):
    """Case-insensitive dedup, preserving first-seen order."""
    seen, out = set(), []
    for it in items:
        k = str(it).strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(str(it).strip())
    return out


def build_proposed(profile: dict, answers: dict) -> dict:
    """Return a copy of `profile` with `skills`/`non_skills` updated from `answers`.

    answers maps a skill name -> "yes" | "no" | "skip" (anything else ignored).
    Yes-skills merge into `skills`; No-skills into `non_skills`; a name answered
    Yes wins over any prior No (dropped from the denylist).
    """
    yes = [s for s, a in answers.items() if str(a).lower() == "yes"]
    no = [s for s, a in answers.items() if str(a).lower() == "no"]

    proposed = dict(profile)
    skills = _dedup(list(profile.get("skills") or []) + yes)
    skill_keys = {s.lower() for s in skills}
    non_skills = [s for s in _dedup(list(profile.get("non_skills") or []) + no)
                  if s.lower() not in skill_keys]
    proposed["skills"] = skills
    proposed["non_skills"] = non_skills
    return proposed


def generate_skills(field: str, exclude: list[str] = None) -> list[str]:
    """Ask Claude for ~100 skills covering one or more fields. Deduped, order preserved.

    When `field` names several roles (comma/"and"-separated), the list is drawn
    roughly evenly from all of them rather than just their overlap. When `exclude`
    is given (an existing list), Claude is asked for 100 NEW skills not already in
    it, and any repeats are filtered out client-side too.
    """
    exclude = exclude or []
    fields = _split_fields(field)
    if len(fields) <= 1:
        prompt = f"Field: {field}\n\n"
    else:
        per = max(1, round(100 / len(fields)))
        prompt = (
            f"Fields ({len(fields)}): {'; '.join(fields)}\n\n"
            f"Draw skills roughly evenly from ALL {len(fields)} fields (about {per} per "
            f"field), covering each field's real skills, not just the ones they share. "
            f"Interleave them so the list is a fair mix.\n\n"
        )
    if exclude:
        prompt += (
            "These skills are ALREADY listed - do NOT repeat any of them or close "
            "variants:\n" + ", ".join(exclude) + "\n\n"
            "Provide about 100 NEW skills not in that list (dig into more specific "
            "tools, niche sub-skills, certifications, and adjacent areas).\n\n"
        )
    prompt += "Generate the ~100-skill inventory JSON now."

    raw, _ = chat_json(SKILLS_SYSTEM, prompt, model=GEN_MODEL, max_tokens=4000)
    out = _dedup(json.loads(raw).get("skills") or [])
    exset = {e.strip().lower() for e in exclude}
    return [s for s in out if s.lower() not in exset]


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Skills Questionnaire</title>
<style>
  body { font: 15px/1.5 system-ui, sans-serif; max-width: 760px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }
  h1 { font-size: 1.4rem; }
  .bar { display: flex; gap: .5rem; margin: 1rem 0; }
  input[type=text] { flex: 1; padding: .5rem .6rem; font-size: 1rem; }
  button { padding: .5rem 1rem; font-size: 1rem; cursor: pointer; }
  #status { color: #666; margin: .5rem 0; }
  .row { display: grid; grid-template-columns: 1fr auto; gap: 1rem; align-items: center;
         padding: .35rem .2rem; border-bottom: 1px solid #eee; transition: background .15s; }
  .row.unanswered { background: #fff6d6; box-shadow: inset 3px 0 0 #e0a800; }
  .opts label { margin-left: .8rem; cursor: pointer; user-select: none; }
  .opts .no { color: #b00; } .opts .yes { color: #070; }
  #save { margin: 1.2rem 0; background: #070; color: #fff; border: 0; border-radius: 4px; }
  #save:disabled { background: #999; }
  .count { position: sticky; top: 0; background: #fff; padding: .4rem 0; border-bottom: 2px solid #ddd; }
  pre { background: #f6f6f6; padding: 1rem; overflow: auto; white-space: pre-wrap; }
</style>
<h1>Skills questionnaire <small style="color:#999">__MODE_NOTE__</small></h1>
<div class="bar">
  <input type="text" id="field" placeholder="Your field(s): one or more, e.g. 'Registered Nurse' or 'dog groomer, vet, bartender, nurse'" autofocus>
  <button id="gen">Generate ~100 skills</button>
</div>
<div id="status"></div>
<div id="list"></div>
<button id="gen-more" style="display:none">Generate another 100</button>
<button id="save" style="display:none">Save</button>
<pre id="out" style="display:none"></pre>
<script>
let skills = [];
const $ = s => document.querySelector(s);

$("#gen").onclick = async () => {
  const field = $("#field").value.trim();
  if (!field) { $("#status").textContent = "Enter a field first."; return; }
  $("#gen").disabled = true; $("#status").textContent = "Asking Claude for ~100 skills...";
  try {
    const r = await fetch("/generate", {method:"POST", body: JSON.stringify({field})});
    if (!r.ok) throw new Error(await r.text());
    skills = (await r.json()).skills;
    render();
    $("#status").textContent = skills.length + " skills. Default is Skip — set Yes on what you have, No on what you don't.";
    $("#save").style.display = "inline-block";
    $("#gen-more").style.display = "inline-block";
  } catch (e) { $("#status").textContent = "Error: " + e.message; }
  $("#gen").disabled = false;
};

// Snapshot the current Yes/No/Skip answers keyed by skill name.
function currentAnswers() {
  const a = {};
  skills.forEach((sk,i)=>{ const c=document.querySelector(`input[name=q${i}]:checked`); a[sk]=c?c.value:"skip"; });
  return a;
}

function render(prev) {
  prev = prev || {};
  const counts = () => {
    let y=0,n=0,s=0;
    document.querySelectorAll(".row").forEach(row=>{
      const v=(row.querySelector("input:checked")||{}).value;
      if(v==="yes")y++; else if(v==="no")n++; else s++;
    });
    $("#count").textContent = `Yes ${y} · No ${n} · Skip ${s} · Total ${skills.length}`;
  };
  const rows = skills.map((sk,i)=>{
    const a = prev[sk] || "skip";
    const ck = v => a===v ? " checked" : "";
    return `
    <div class="row"><span>${sk.replace(/</g,"&lt;")}</span>
      <span class="opts">
        <label class="yes"><input type="radio" name="q${i}" value="yes"${ck("yes")}>Yes</label>
        <label class="no"><input type="radio" name="q${i}" value="no"${ck("no")}>No</label>
        <label><input type="radio" name="q${i}" value="skip"${ck("skip")}>Skip</label>
      </span></div>`;
  }).join("");
  $("#list").innerHTML = `<div class="count" id="count"></div>` + rows;
  const mark = row => { const c=row.querySelector("input:checked"); row.classList.toggle("unanswered", !c || c.value==="skip"); };
  document.querySelectorAll(".row input").forEach(el=>el.onchange=()=>{counts(); mark(el.closest(".row"));});
  document.querySelectorAll(".row").forEach(mark);
  counts();
}

$("#gen-more").onclick = async () => {
  const field = $("#field").value.trim();
  if (!field) { $("#status").textContent = "Enter a field first."; return; }
  const btn = $("#gen-more"); btn.disabled = true;
  $("#status").textContent = "Asking Claude for 100 more (excluding the current list)...";
  try {
    const r = await fetch("/generate", {method:"POST", body: JSON.stringify({field, exclude: skills})});
    if (!r.ok) throw new Error(await r.text());
    const fresh = (await r.json()).skills;
    const have = new Set(skills.map(s=>s.toLowerCase()));
    const added = fresh.filter(s=>!have.has(s.toLowerCase()));
    const prev = currentAnswers();          // keep answers already given
    skills = skills.concat(added);
    render(prev);
    $("#status").textContent = `Added ${added.length} new skills (total ${skills.length}). Existing answers kept.`;
  } catch (e) { $("#status").textContent = "Error: " + e.message; }
  btn.disabled = false;
};

$("#save").onclick = async () => {
  const answers = {};
  skills.forEach((sk,i)=>{
    const v=(document.querySelector(`input[name=q${i}]:checked`)||{}).value||"skip";
    answers[sk]=v;
  });
  $("#save").disabled = true; $("#status").textContent = "Saving...";
  const r = await fetch("/save", {method:"POST", body: JSON.stringify({field:$("#field").value.trim(), answers})});
  const j = await r.json();
  $("#out").style.display="block";
  $("#out").textContent = (j.applied ? "SAVED to data/user_profile.yaml.\\n\\n" : "DRY RUN — data/user_profile.yaml NOT modified.\\n\\n")
    + `skills now (${j.skills_total}), added ${j.added_skills.length}: ${j.added_skills.join(", ")||"(none)"}\\n\\n`
    + `non_skills / denylist (${j.non_skills.length}): ${j.non_skills.join(", ")||"(none)"}\\n\\n`
    + `Written to:\\n${j.path}`;
  $("#status").textContent = j.server_stopping ? "Saved to your profile. Server stopped - you can close this tab."
    : (j.applied ? "Saved to your profile. You can close this tab." : "Dry-run proposal written (real profile untouched).");
  $("#save").disabled = false;
};
</script>
"""


def _make_handler(apply: bool, proposed_path: str, shutdown_on_save: bool):
    class Handler(BaseHTTPRequestHandler):
        def _deferred_shutdown(self):
            # Let the HTTP response flush to the browser, then stop the server.
            time.sleep(0.5)
            self.server.shutdown()

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body_json(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if self.path == "/":
                note = "(saves to your profile)" if apply else "(dry run)"
                self._send(200, PAGE.replace("__MODE_NOTE__", note), "text/html; charset=utf-8")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            try:
                if self.path == "/generate":
                    data = self._body_json()
                    field = (data.get("field") or "").strip()
                    if not field:
                        return self._send(400, "field is required", "text/plain")
                    exclude = data.get("exclude") or []
                    print(f"[skills] generating for field={field!r} (excluding {len(exclude)})")
                    skills = generate_skills(field, exclude=exclude)
                    print(f"[skills] {len(skills)} new skills")
                    return self._send(200, json.dumps({"skills": skills}))

                if self.path == "/save":
                    data = self._body_json()
                    field = (data.get("field") or "").strip()
                    answers = data.get("answers") or {}
                    profile = load_profile()
                    before = list(profile.get("skills") or [])
                    proposed = build_proposed(profile, answers)
                    added = [s for s in proposed["skills"] if s not in before]

                    if apply:
                        save_profile(proposed)
                        path = os.path.abspath(
                            os.path.join(os.path.dirname(__file__), "..", "data", "user_profile.yaml"))
                    else:
                        with open(proposed_path, "w", encoding="utf-8") as f:
                            yaml.dump(proposed, f, sort_keys=False, allow_unicode=True)
                        path = proposed_path

                    print("\n" + "=" * 60)
                    print(f"RESULT: {'SAVED' if apply else 'dry-run'} | field={field!r}")
                    print(f"  answers: yes={sum(1 for v in answers.values() if v=='yes')} "
                          f"no={sum(1 for v in answers.values() if v=='no')} "
                          f"skip={sum(1 for v in answers.values() if v=='skip')}")
                    print(f"  skills total: {len(proposed['skills'])} (added {len(added)})")
                    print(f"  non_skills:   {len(proposed['non_skills'])}")
                    print(f"  wrote -> {path}")
                    if not apply:
                        print("  data/user_profile.yaml NOT modified")
                    print("=" * 60)

                    self._send(200, json.dumps({
                        "applied": apply,
                        "path": path,
                        "added_skills": added,
                        "skills_total": len(proposed["skills"]),
                        "non_skills": proposed["non_skills"],
                        "server_stopping": shutdown_on_save,
                    }))
                    if shutdown_on_save:
                        print("  [skills] Save received - shutting down server.")
                        threading.Thread(target=self._deferred_shutdown, daemon=True).start()
                    return

                self._send(404, "not found", "text/plain")
            except Exception as e:
                print(f"[error] {type(e).__name__}: {e}")
                self._send(502, f"{type(e).__name__}: {e}", "text/plain")

        def log_message(self, *a):
            pass

    return Handler


def serve(apply: bool = False, port: int = PORT, proposed_path: str = None,
          shutdown_on_save: bool = None):
    """Start the questionnaire web UI. Blocks until it stops.

    apply=True  -> Save writes the real profile.
    apply=False -> Save writes `proposed_path` (dry run); real profile untouched.
    shutdown_on_save -> stop the server after a successful Save. Defaults to
        `apply` (setup flow dies on Save; the dry-run harness stays up to iterate).
    """
    if proposed_path is None:
        proposed_path = os.path.join(os.path.dirname(__file__), "..",
                                     "test-questionarre", "proposed_changes.yaml")
    if shutdown_on_save is None:
        shutdown_on_save = apply
    mode = "SAVES TO PROFILE" if apply else "DRY RUN (no profile writes)"
    print(f"Skills questionnaire [{mode}] -> http://localhost:{port}")
    if shutdown_on_save:
        print("Fill it out in your browser and click Save. The server stops automatically on Save.")
    else:
        print("Fill it out in your browser, click Save, then Ctrl-C here to continue.")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(apply, proposed_path, shutdown_on_save))
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        if shutdown_on_save:
            print("Skills questionnaire server stopped.")


if __name__ == "__main__":
    serve(apply=True)
