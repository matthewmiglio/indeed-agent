"""Durable per-application records: what we actually submitted, for each job.

Written on every LIVE submit so that if an employer follows up, there's a record
of the resume used and every answer given. Saved under data/applications/ as a
JSON (complete) plus a readable .md twin. Not debug/throwaway — keep these.
"""
import os
import re
import json
import datetime

REC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "applications"))


def _slug(s: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "job")[:50]


def save_application(job: dict, answers: dict, resume_pdf: str, status: str) -> str:
    """Write one application record; return the .md path."""
    os.makedirs(REC_DIR, exist_ok=True)
    now = datetime.datetime.now()
    company = (job or {}).get("company") or "unknown"
    title = (job or {}).get("title") or "unknown"
    base = f"{now:%Y%m%d-%H%M%S}_{_slug(company)}"
    rec = {
        "company": company,
        "title": title,
        "url": (job or {}).get("job_url") or (job or {}).get("url") or "",
        "submitted_at": now.isoformat(timespec="seconds"),
        "status": status,
        "resume_pdf": resume_pdf or "",
        "salary_posted": (job or {}).get("salary") or "",
        "answers": {str(k): str(v) for k, v in (answers or {}).items()},
    }
    with open(os.path.join(REC_DIR, base + ".json"), "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2)

    lines = [
        f"# {company} - {title}", "",
        f"- Submitted: {rec['submitted_at']}  (status: {status})",
        f"- Job URL: {rec['url']}",
        f"- Posted pay: {rec['salary_posted'] or 'n/a'}",
        f"- Resume used: {rec['resume_pdf'] or 'n/a'}",
        "", "## Answers submitted", "",
    ]
    for k, v in rec["answers"].items():
        if k == "resume":
            continue
        lines.append(f"- **{k}**: {v}")
    md_path = os.path.join(REC_DIR, base + ".md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return md_path


if __name__ == "__main__":  # tiny self-check
    import tempfile
    REC_DIR = tempfile.mkdtemp()
    p = save_application(
        {"company": "Acme Co", "title": "SWE", "job_url": "http://x", "salary": "$200,000"},
        {"resume": "/r.pdf", "Desired salary": "200000", "Authorized?": "Yes"},
        "/r.pdf", "applied")
    txt = open(p, encoding="utf-8").read()
    assert "Acme Co" in txt and "200000" in txt and "Authorized?" in txt
    assert os.path.exists(p[:-3] + ".json")
    print("OK ->", p)
