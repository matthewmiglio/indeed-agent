"""Per-job tuned resume generation with a visual validation loop.

Flow for one job:
  1. Load the candidate's ground-truth corpus (base resume JSON + docs/*.md + profile).
  2. Ask Opus to tailor {title, summary, sections} to THIS job's description,
     grounded ONLY in the corpus (no fabrication), aiming for exactly one page.
  3. Render JSON -> PDF via the vendored gen_resume, rasterize page 1 -> PNG.
  4. Validate: page count must be 1 (deterministic) + Haiku vision spots layout issues.
  5. If not clean and iterations remain, feed the issues back to Opus and re-render.
  6. Return the path to the BEST attempt's PDF (best = fits one page, then fewest issues).

Any failure returns None so the caller falls back to the static resume and still applies.

Contact line (name/email/links/icons) is fixed from base_resume.json — the model never
touches it. Icon paths are normalized to `assets/*.png` and resolve against RESUME_GEN_DIR.
"""

import json
import os
import re
import sys
from pathlib import Path

import fitz  # PyMuPDF

from claude_client import chat_json
from job_scorer import _build_profile_summary  # reuse the profile condenser

_REPO_ROOT = Path(__file__).resolve().parent.parent
RESUME_GEN_DIR = _REPO_ROOT / "resume_gen"
BASE_RESUME_PATH = RESUME_GEN_DIR / "base_resume.json"
DATA_RESUMES_DIR = _REPO_ROOT / "data" / "resumes"

# The candidate's deep history (personal, not vendored). Env-overridable.
RESUME_CONTENT_DIR = Path(
    os.environ.get("RESUME_CONTENT_DIR", str(Path.home() / "Downloads" / "resume-work"))
)

GEN_MODEL = "claude-opus-4-8"
VISION_MODEL = "claude-haiku-4-5"
MAX_ITERS = 3

sys.path.insert(0, str(RESUME_GEN_DIR))
import gen_resume  # noqa: E402  (vendored renderer; provides build())


GEN_SYSTEM = """You tailor Matthew Miglio's one-page resume to a specific job posting for an automated application agent. You output ONLY the parts that change: title, summary, and sections. Contact info and name are handled elsewhere.

Your ONLY source of truth is the CANDIDATE CORPUS in the user message (a base resume plus background docs). This is an ironclad rule: every company, title, date, bullet, skill, and metric you emit must be present in that corpus. Never invent an employer, project, number, or credential. You may rephrase, reorder, condense, and choose WHICH real facts to feature — you may not add facts that aren't there.

PER-EMPLOYER ATTRIBUTION (strict, most-violated rule — obey it literally): each employer entry may ONLY contain facts the corpus attributes to THAT employer. This includes the job TITLE: use the title the corpus gives that employer verbatim; never rename a role to fit the posting. RELEVANCE TO THE JOB NEVER JUSTIFIES MOVING A FACT BETWEEN EMPLOYERS. Before you emit any bullet, check: does the corpus name this exact technology/tool/metric UNDER THIS EXACT EMPLOYER? If not, delete it.
The single most common violation is cloud/infra services (AWS, Lambda, S3, RDS, "distributed cloud services", "cloud backends") and named tools bled from one employer onto another. Concrete case you must get right: the corpus places AWS/Lambda/S3 cloud-data-pipeline work under IAOB, and describes the automotive/perception employer as OFF-CHIP / EMBEDDED / ON-DEVICE camera-perception and real-time inference for validation and benchmarking — NOT cloud work. So an automotive-perception entry must NEVER say "AWS", "Lambda", "distributed cloud services", "cloud backends", or "telemetry pipelines"; feature its real grounded bullets (perception, occupant/driver monitoring, off-chip inference, drowsiness profiling) instead. When unsure which employer a fact belongs to, leave it out. Do not synthesize a plausible bullet by combining an employer's domain with a tech stack borrowed from elsewhere.

SKILLS ALLOWLIST (strict): the "Skills:" line in the PROFILE SUMMARY is the AUTHORITATIVE list of programming languages, frameworks, and tools the candidate will claim. In the TECHNICAL SKILLS section — and anywhere else (title, summary, bullets) — never name a language, framework, or tool that is not in that Skills list, even if a background doc mentions it. If the corpus and the profile Skills list disagree, the profile Skills list wins. Do not "round up" a tool the candidate merely touched into a claimed skill.

SKILLS DENYLIST (strict): if a "SKILLS DENYLIST" block appears in the corpus, never name any of those skills or tools anywhere (title, summary, bullets, skills section), even if a background doc mentions them — the candidate has explicitly confirmed they do NOT have these. The denylist overrides everything else.

Tailoring goal: surface the corpus experience most relevant to THIS job. Rewrite the title line and summary to match the role, reorder and trim bullets to lead with what the posting asks for, and order the skills section by relevance to the posting. Wrap posting-relevant keywords that are genuinely backed by the corpus in **double asterisks** for bold.

SKILLS SECTION GROUNDING (strict). Two kinds of skill entries, judged differently:
1. Any NAMED technology, tool, language, framework, platform, library, database, architecture pattern/style, domain standard, certification, or methodology (e.g. Microservices, Kubernetes, Elasticsearch, GraphQL, Kafka, Accessibility/WCAG, Scrum/Agile, Terraform) — list it ONLY if that exact name (or an obvious alias) appears in the corpus. The posting asking for it is NEVER a reason to add it. If the candidate lacks it, omit it; listing it is a fabrication.
2. Generic engineering practices any professional software engineer legitimately claims (System design, Code review, Mentoring, Unit/Integration/End-to-end testing, Debugging) are allowed even if not spelled out in the corpus.
When unsure which kind an entry is, treat it as kind 1 and drop it. Never pad the skills list with posting keywords to look like a match.

WORK EXPERIENCE ORDER: list work-experience entries in reverse-chronological order (most recent / current role first, by end date — "Present" is newest). Reorder BULLETS within a role and the SKILLS section for relevance, but NEVER reorder the roles themselves out of date order; leading with an older role looks like a mistake.

Do NOT wrap the summary (or any field) in single asterisks for italic — the renderer italicizes the summary by style. Use asterisks ONLY as **double** for bold.

Hard constraint: the result MUST fit on ONE Letter page. Be ruthless — prefer 3-4 strong experience entries with 1-3 tight bullets each plus a skills section over exhaustive coverage. If asked to revise for overflow, cut the least-relevant content.

Return ONLY valid JSON with EXACTLY these keys:
{
  "title": "role line, e.g. 'Backend Software Engineer — Python & AWS'",
  "summary": "one italic blurb, 2-3 sentences, **bold** key terms",
  "sections": [
    { "heading": "EDUCATION", "type": "entries", "items": [ { "place": "...", "date": "..." } ] },
    { "heading": "PROFESSIONAL EXPERIENCE", "type": "entries",
      "items": [ { "place": "Company — Title", "date": "...", "bullets": ["..."] } ] },
    { "heading": "TECHNICAL SKILLS & TOOLS", "type": "skills",
      "items": [ { "label": "Languages", "value": "..." } ] }
  ]
}
No prose, no markdown fences — just the JSON object."""

VISION_SYSTEM = """You are a resume layout QA reviewer. You are shown a PNG render of a one-page resume. Judge ONLY visual quality, not content wording.

Flag concrete problems: content overflowing or cut off, text running off the page edge, an obviously empty/blank lower half (too sparse), badly cramped spacing, dates misaligned with their entries, a heading orphaned at the very bottom with nothing under it, or garbled/overlapping text.

Return ONLY valid JSON:
{
  "ok": true | false,
  "issues": ["short, specific, actionable problem", "..."]
}
ok=true means it looks like a clean, professional, well-balanced one-page resume. If ok is true, issues must be []. Be strict but do not invent nitpicks."""


def _slug(job: dict) -> str:
    key = job.get("job_key")
    if key:
        return re.sub(r"[^A-Za-z0-9_-]", "", str(key))[:60]
    raw = f"{job.get('company', 'company')}-{job.get('title', 'role')}"
    return re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()[:60] or "job"


def _fixed_contact(base: dict) -> list:
    """Copy the base contact block, normalizing icon paths to assets/<file>.png."""
    out = []
    for it in base.get("contact", []):
        it = dict(it)
        if it.get("icon"):
            it["icon"] = "assets/" + Path(it["icon"]).name
        out.append(it)
    return out


def _load_corpus(profile: dict) -> str:
    """Concatenate the base resume, docs/*.md, and a profile summary as ground truth."""
    parts = ["=== BASE RESUME (JSON) ===", BASE_RESUME_PATH.read_text(encoding="utf-8")]
    docs_dir = RESUME_CONTENT_DIR / "docs"
    if docs_dir.is_dir():
        budget = 12000
        for md in sorted(docs_dir.glob("*.md")):
            text = md.read_text(encoding="utf-8", errors="replace")[:budget]
            parts.append(f"\n=== {md.name} ===\n{text}")
            budget -= len(text)
            if budget <= 0:
                break
    summary = _build_profile_summary(profile)
    if summary:
        parts.append(f"\n=== PROFILE SUMMARY ===\n{summary}")
    jobs = [j for j in (profile.get("work_history") or []) if isinstance(j, dict) and j]
    if jobs:
        lines = []
        for j in jobs:
            head = " | ".join(p for p in (
                j.get("job_title", ""), j.get("employer", ""), j.get("location", ""),
                f"{j.get('start_date', '')} to {j.get('end_date') or 'Present'}".strip(" to"),
            ) if p)
            lines.append(f"- {head}")
            if j.get("summary"):
                lines.append(f"    {j['summary']}")
        parts.append("\n=== WORK HISTORY (candidate-entered) ===\n" + "\n".join(lines))
    non_skills = profile.get("non_skills") or []
    if non_skills:
        joined = ", ".join(str(s) for s in non_skills)
        parts.append(f"\n=== SKILLS DENYLIST (never claim these) ===\n{joined}")
    return "\n".join(parts)


def _generate(corpus: str, job: dict, model: str) -> dict:
    """First-pass tailored {title, summary, sections} from Opus."""
    prompt = (
        f"CANDIDATE CORPUS:\n{corpus}\n\n"
        f"TARGET JOB:\nTitle: {job.get('title', '')}\n"
        f"Company: {job.get('company', '')}\n"
        f"Description:\n{job.get('description', '')[:4000]}\n\n"
        "Produce the tailored title, summary, and sections JSON now."
    )
    raw, _ = chat_json(GEN_SYSTEM, prompt, model=model, max_tokens=4000)
    return json.loads(raw)


def _revise(prev: dict, issues: list, job: dict, model: str) -> dict:
    """Revise the previous JSON to fix reported layout issues."""
    prompt = (
        f"TARGET JOB: {job.get('title', '')} at {job.get('company', '')}\n\n"
        f"Your previous resume JSON:\n{json.dumps(prev)}\n\n"
        f"Layout problems found in the rendered PDF:\n- " + "\n- ".join(issues) + "\n\n"
        "Return the corrected JSON (same schema: title, summary, sections). "
        "Fix the problems above; if it overflowed one page, cut the least-relevant "
        "content. Keep everything grounded in the corpus facts you used before."
    )
    raw, _ = chat_json(GEN_SYSTEM, prompt, model=model, max_tokens=4000)
    return json.loads(raw)


def _strip_wrap_italic(s: str) -> str:
    """Remove a single *...* italic wrap the LLM sometimes adds around a field.
    Preserves **bold** (two asterisks); only peels a lone leading/trailing '*'.
    """
    s = s.strip()
    if s.startswith("*") and not s.startswith("**"):
        s = s[1:]
    if s.endswith("*") and not s.endswith("**"):
        s = s[:-1]
    return s.strip()


def _render(data: dict, out_dir: Path) -> tuple[Path, Path, int]:
    """Write resume.json, render resume.pdf, rasterize preview.png. Returns (pdf, png, pages)."""
    # Peel stray single-asterisk italic wraps (the renderer handles **bold** but
    # leaves a lone wrapping '*' as literal text — seen on the summary line).
    for k in ("title", "summary"):
        if isinstance(data.get(k), str):
            data[k] = _strip_wrap_italic(data[k])
    json_path = out_dir / "resume.json"
    pdf_path = out_dir / "resume.pdf"
    png_path = out_dir / "preview.png"
    json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    gen_resume.build(data, pdf_path, RESUME_GEN_DIR)  # base= for icon resolution
    doc = fitz.open(str(pdf_path))
    pages = doc.page_count
    doc[0].get_pixmap(dpi=150).save(str(png_path))
    doc.close()
    return pdf_path, png_path, pages


def _validate(png_path: Path, pages: int, vision_model: str) -> tuple[bool, list]:
    """Deterministic page-count check + Haiku vision review. Returns (ok, issues)."""
    issues = []
    if pages != 1:
        issues.append(f"Resume renders as {pages} pages; it must fit on exactly 1 page.")
    try:
        raw, _ = chat_json(VISION_SYSTEM, "Review this resume render.",
                           model=vision_model, max_tokens=600, image_path=str(png_path))
        vis = json.loads(raw)
        issues += [str(x) for x in vis.get("issues", [])]
        vis_ok = bool(vis.get("ok"))
    except Exception as e:
        print(f"  [resume] vision check failed ({e}); relying on page count")
        vis_ok = True  # don't block on a flaky vision call
    ok = (pages == 1) and vis_ok  # one-page fit is mandatory
    return ok, issues


def build_tuned_resume(job: dict, profile: dict, out_dir: str = None,
                       model: str = GEN_MODEL, vision_model: str = VISION_MODEL,
                       max_iters: int = MAX_ITERS) -> str | None:
    """Craft, validate, and iterate a resume tuned to `job`. Returns best PDF path or None."""
    try:
        out = Path(out_dir) if out_dir else DATA_RESUMES_DIR / _slug(job)
        out.mkdir(parents=True, exist_ok=True)

        base = json.loads(BASE_RESUME_PATH.read_text(encoding="utf-8"))
        fixed = {"name": base["name"], "contact": _fixed_contact(base)}
        corpus = _load_corpus(profile)

        tailored = _generate(corpus, job, model)
        best = None  # (rank_tuple, data)
        for i in range(max_iters):
            data = {**fixed, **tailored}
            pdf_path, png_path, pages = _render(data, out)
            ok, issues = _validate(png_path, pages, vision_model)
            rank = (0 if pages == 1 else 1, len(issues))  # lower is better
            print(f"  [resume] attempt {i+1}: {pages} page(s), "
                  f"{len(issues)} issue(s){' — OK' if ok else ''}")
            if best is None or rank < best[0]:
                best = (rank, data)
            if ok:
                break
            if i < max_iters - 1:
                tailored = _revise(tailored, issues, job, model)

        # Re-render the best attempt so the final files on disk are the one we return.
        pdf_path, _, _ = _render(best[1], out)
        print(f"  [resume] tuned resume ready: {pdf_path}")
        return str(pdf_path)
    except Exception as e:
        print(f"  [resume] tuning failed ({type(e).__name__}: {e})")
        return None


def demo():
    """Self-check: tune a sample backend job and assert a PDF + PNG land on disk."""
    from profile_manager import load_profile
    job = {
        "title": "Backend Software Engineer",
        "company": "Acme Data Co",
        "job_key": "_demo",
        "description": (
            "We need a backend engineer strong in Python, Flask, and AWS (Lambda, S3, RDS). "
            "You'll build data pipelines and CI/CD. Bonus: LLM/agent experience."
        ),
    }
    pdf = build_tuned_resume(job, load_profile(), out_dir=str(DATA_RESUMES_DIR / "_demo"))
    assert pdf and Path(pdf).exists(), f"no PDF produced: {pdf}"
    doc = fitz.open(pdf)
    print(f"demo ok: {pdf} ({doc.page_count} page(s))")
    doc.close()


if __name__ == "__main__":
    demo()
