"""Pre-submit answer sanity check.

Two layers, run right before the submit gate:
  A. Deterministic, free checks (em/en dashes, agent-speak/placeholders,
     company-name verbatim in "why this company" answers, disability/sponsorship
     invariants against the profile).
  B. One batched Claude review of every (label, value) pair against the profile
     and job description, returning ok | wrong | ungrounded per answer.

sanity_check() returns (ok, issues). In live mode the caller blocks submit when
not ok; in dry-run it just prints the issues.
"""

import json
import re

from claude_client import chat_json

# Trailing corporate suffixes to ignore when finding a company's distinctive token.
_CORP_SUFFIXES = {"inc", "llc", "lp", "ltd", "corp", "co", "group", "advisory",
                  "the", "systems", "industries", "technologies", "labs", "ai"}


def _company_token(company: str) -> str:
    """The distinctive word to look for (first non-generic token, lowercased)."""
    for tok in company.lower().replace(",", " ").split():
        if tok.strip("., ") and tok.strip("., ") not in _CORP_SUFFIXES:
            return tok.strip("., ")
    return company.lower().strip()

# Keys in all_answers that are bookkeeping, not real answers to review.
_SKIP_KEYS = {"resume", "resume_not_used", "_sanity_issues"}

_AGENT_SPEAK = (
    "the candidate", "the applicant", "as an ai", "as a language model",
    "i am an ai", "[company]", "[role]", "[position]", "[name]", "placeholder",
    "lorem ipsum",
)


def _is_free_text(value: str) -> bool:
    """A written response, not a short field value."""
    return len(value) > 60 or value.count(" ") >= 8


def layer_a_checks(answers: dict, profile: dict, job_data: dict) -> list[str]:
    """Deterministic checks. Returns human-readable issue strings (empty = clean)."""
    issues = []
    company = (job_data.get("company") or "").strip()
    for label, value in answers.items():
        if label in _SKIP_KEYS or not isinstance(value, str) or not value.strip():
            continue
        low, llabel = value.lower(), label.lower()

        # 1. No em/en dashes in anything sent to an employer.
        if "—" in value or "–" in value:
            issues.append(f"em/en dash in answer: {label!r}")

        # 2. Agent-speak / unfilled placeholders.
        hit = next((b for b in _AGENT_SPEAK if b in low), None)
        if hit:
            issues.append(f"agent-speak/placeholder {hit!r} in answer: {label!r}")

        # 3. Free-text "why this company" must name the company verbatim. Match
        # the distinctive token as a WHOLE WORD, so "Brilliant" does not satisfy
        # a check for "Brillian" (a substring test would wrongly pass).
        if (company and _is_free_text(value)
                and any(k in llabel for k in ("compan", "why", "interest",
                                              "work at", "work for"))):
            tok = _company_token(company)
            if tok and not re.search(r"\b" + re.escape(tok) + r"\b", low):
                issues.append(f"free-text answer to {label!r} omits the company name "
                              f"{company!r} (looked for {tok!r} as a whole word)")

        # 4. Disability must never be answered Yes.
        if "disability" in llabel and low.strip() in ("yes", "y"):
            issues.append(f"disability answered Yes: {label!r}")

        # 5. Sponsorship (yes/no) must match the profile.
        if "sponsor" in llabel and profile.get("sponsorship_needed") is not None \
                and low.strip() in ("yes", "no"):
            want = "yes" if profile.get("sponsorship_needed") else "no"
            if low.strip() != want:
                issues.append(f"sponsorship answer {value!r} contradicts profile "
                              f"(sponsorship_needed={profile.get('sponsorship_needed')}): {label!r}")
    return issues


_REVIEW_SYSTEM = """You review a job application right before it is submitted. You get the candidate's profile (JSON), the job (company + description), and the answers that were filled in. For EACH answer decide:
- "ok": correct, consistent with the profile, and appropriate for the question.
- "wrong": factually wrong, self-contradictory, contradicts the profile, or names the wrong company.
- "ungrounded": claims experience, skills, or facts not supported by the profile.
The ENTIRE profile is grounding, prose fields included: experience_summary and the current/past job titles count exactly as much as the structured skills array. If a claim is supported ANYWHERE in the profile, it is grounded. Concretely: experience_summary says the candidate "builds multi-agent LLM systems" and spans reinforcement learning and computer vision, so an answer mentioning LLM / multi-agent / RAG / ML work is grounded even though "LLM" is not a line in the skills array. Do NOT flag a claim ungrounded merely because it is absent from the skills list; read the whole profile first.
A truthful answer is always "ok", even when it is unfavorable to the candidate (e.g. "No" to a requirement they genuinely do not meet, a lower salary, no relocation). NEVER flag an honest answer for hurting the application — we do not lie to improve odds. Judge truthfulness strictly, but do NOT flag an answer merely for being brief, confident, or unfavorable. Return ONLY JSON:
{"verdicts": [{"label": "<field label>", "status": "ok|wrong|ungrounded", "reason": "<one short line, only when status is not ok>"}]}"""


def layer_b_review(answers: dict, profile: dict, job_data: dict,
                   model: str = "claude-haiku-4-5") -> list[str]:
    """One batched Claude review. Returns issue strings for non-ok verdicts.

    Fails open: on any error (network, bad JSON) returns [] with a printed note,
    so a reviewer hiccup never blocks a correct application.
    """
    pairs = {k: v for k, v in answers.items()
             if k not in _SKIP_KEYS and isinstance(v, str) and v.strip()}
    if not pairs:
        return []
    jd = (job_data.get("description") or job_data.get("job_description") or "")[:2500]
    prompt = (
        f"Company: {job_data.get('company', 'unknown')}\n"
        f"Job title: {job_data.get('title', 'unknown')}\n"
        f"Job description:\n{jd}\n\n"
        f"Candidate profile (JSON):\n{json.dumps(profile, default=str)[:6000]}\n\n"
        f"Answers filled in (label -> value):\n{json.dumps(pairs, indent=1, default=str)}"
    )
    try:
        raw, _ = chat_json(_REVIEW_SYSTEM, prompt, model=model, max_tokens=1200)
        verdicts = json.loads(raw).get("verdicts", [])
    except Exception as e:
        print(f"  [sanity] Layer B review skipped (error: {e})")
        return []
    issues = []
    for v in verdicts:
        status = str(v.get("status", "ok")).lower()
        if status not in ("ok", ""):
            issues.append(f"[{status}] {v.get('label', '?')!r}: {v.get('reason', '').strip()}")
    return issues


def sanity_check(answers: dict, profile: dict, job_data: dict,
                 model: str = "claude-haiku-4-5") -> tuple[bool, list[str]]:
    """Run both layers. Returns (ok, issues); ok is True when issues is empty."""
    issues = layer_a_checks(answers, profile, job_data)
    issues += layer_b_review(answers, profile, job_data, model=model)
    return (not issues), issues


def demo():
    """Self-check for Layer A (no network). Layer B needs Claude, tested live."""
    prof = {"sponsorship_needed": False, "disability_status": "No"}
    job = {"company": "Brillian", "title": "SWE"}
    bad = {
        "Why do you want to work here?": "I'm excited to apply to Brilliant because the candidate loves AI.",
        "Do you require sponsorship?": "Yes",
        "Do you have a disability?": "Yes",
        "Reason": "I built systems — with great results.",
    }
    issues = layer_a_checks(bad, prof, job)
    # Expect: wrong company (Brillian not in text), agent-speak, sponsorship, disability, em dash.
    assert any("company name" in i for i in issues), issues
    assert any("agent-speak" in i for i in issues), issues
    assert any("sponsorship" in i for i in issues), issues
    assert any("disability" in i for i in issues), issues
    assert any("dash" in i for i in issues), issues

    good = {"Why Brillian?": "I admire Brillian's work in production AI systems and want to contribute."}
    assert layer_a_checks(good, prof, job) == [], layer_a_checks(good, prof, job)
    print(f"ok - layer A caught {len(issues)} issues on the bad set, 0 on the good set")


if __name__ == "__main__":
    demo()
