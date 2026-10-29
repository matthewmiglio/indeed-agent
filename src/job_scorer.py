"""Job posting scorer that evaluates how well a listing matches the candidate.

Sends job data plus the user's profile and search criteria to Claude (Haiku),
which returns a 0-100 score and a pass/fail verdict.
"""

import json
import time
from claude_client import chat_json

SYSTEM_PROMPT = """You evaluate how well ONE Indeed job posting matches a candidate, for an automated application agent. Your verdict gates whether the agent actually applies, so be decisive and calibrated — judge only on the profile and criteria given, and never assume qualifications the candidate did not state.

Return ONLY valid JSON:
{
  "score": number 0-100,
  "pass": true | false,
  "reasoning": "one or two sentences naming the deciding factor"
}

Step 1 — hard rules. If ANY of these fire, set pass=false regardless of score:
- The job requires core skills the candidate clearly lacks and could not learn quickly.
- The job is in a different field from the candidate's experience.
- A salary is listed and is meaningfully below the candidate's stated minimum.
- The job matches one of the candidate's exclusions.
- The job requires a license/certification the candidate does not hold.

Set pass=false whenever a hard rule fires. If NO hard rule fires, set pass=true; a numeric cutoff is applied downstream, so you do not decide the threshold — just score honestly.

Step 2 — score the match on this scale:
- 80-100: strong skill and experience-level fit; location/remote and salary aligned.
- 60-79: solid fit with only minor gaps.
- 40-59: weak or uncertain fit (partial skill match, vague posting).
- 0-39: poor fit.
Adjustments: reputable/well-known company -> small boost; vague, generic, or spammy posting -> lower.

When information is missing, do not penalize as if it were a negative — score on what is present and say so in the reasoning. Return ONLY the JSON.
"""


def _build_profile_summary(profile: dict) -> str:
    """Condense the user profile into a brief text summary for the LLM prompt."""
    parts = []
    name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
    if name:
        parts.append(f"Name: {name}")
    if profile.get("current_job_title"):
        parts.append(f"Current role: {profile['current_job_title']}")
    if profile.get("years_experience") is not None:
        parts.append(f"Experience: {profile['years_experience']} years")
    if profile.get("experience_summary"):
        parts.append(f"Background: {profile['experience_summary']}")
    if profile.get("highest_education"):
        edu = profile["highest_education"]
        if profile.get("degree_field"):
            edu += f" in {profile['degree_field']}"
        parts.append(f"Education: {edu}")
    if profile.get("skills"):
        skills = profile["skills"]
        if isinstance(skills, list):
            skills = ", ".join(skills)
        parts.append(f"Skills: {skills}")
    if profile.get("desired_salary"):
        parts.append(f"Desired salary: ${profile['desired_salary']}")
    if profile.get("work_type_preference"):
        parts.append(f"Work preference: {profile['work_type_preference']}")
    return "\n".join(parts)


def score_job(job_data: dict, profile: dict, criteria: dict, model: str = "claude-haiku-4-5",
              min_score: int = 60) -> dict:
    """Score a job posting against the candidate's profile and search criteria.

    Args:
        job_data: Extracted job details (title, company, description, etc.).
        profile: User profile dict from profile_manager.
        criteria: Parsed intent dict from the prompt parser.
        model: Claude model ID for evaluation.
        min_score: Score cutoff to pass. Final pass = model didn't fail a hard
            rule AND score >= min_score. Tune per run without editing the prompt.

    Returns:
        Dict with 'score' (0-100), 'pass' (bool), and 'reasoning' (str).
    """
    title = job_data.get("title", "unknown")
    company = job_data.get("company", "unknown")
    salary = job_data.get("salary", "not listed")
    exclusions = criteria.get("exclusions")
    profile_summary = _build_profile_summary(profile)

    prompt = f"""Candidate Profile:
{profile_summary}

Search Criteria:
Looking for: {criteria.get('job_title', 'any')}
Experience level: {criteria.get('experience_level', 'any')}
Remote preference: {criteria.get('remote_preference', 'any')}
Minimum salary: {criteria.get('salary_min') or 'none specified'}
Exclusions: {exclusions or 'none'}

Job Posting:
Title: {title}
Company: {company}
Location: {job_data.get('location', 'unknown')}
Salary: {salary}
Job Type: {job_data.get('job_type', 'unknown')}

Description:
{job_data.get('description', 'none')[:2000]}
"""

    safe_title = title.encode("ascii", errors="replace").decode("ascii")
    print(f"  [scorer] Evaluating: \"{safe_title}\" at {company}")
    if exclusions:
        print(f"  [scorer] Exclusions: {exclusions}")
    t0 = time.time()

    raw, tokens = chat_json(SYSTEM_PROMPT, prompt, model=model)

    elapsed = time.time() - t0
    safe_raw = raw.encode("ascii", errors="replace").decode("ascii")
    print(f"  [scorer] Model responded in {elapsed:.1f}s ({tokens} tokens)")
    print(f"  [scorer] Raw output: {safe_raw}")

    result = json.loads(raw)
    result.setdefault("score", 50)
    result.setdefault("pass", True)
    result.setdefault("reasoning", "")

    # Hard-rule fail from the model stays a fail; otherwise gate on the score cutoff.
    result["pass"] = bool(result["pass"]) and result["score"] >= min_score

    # Deterministic salary floor (user policy): if a minimum salary was specified
    # and this posting lists NO salary, dump it — we can't confirm it clears the
    # floor. Only applies when the user actually set a floor; otherwise unposted
    # pay is fine and goes to the model's judgment above.
    sal = str(job_data.get("salary") or "").strip()
    if criteria.get("salary_min") and not sal:
        result["pass"] = False
        result["reasoning"] = (
            f"No salary listed and a minimum of ${criteria['salary_min']} was required, "
            "so this unposted-pay job is skipped. " + result.get("reasoning", "")
        ).strip()

    verdict = "PASS" if result["pass"] else "FAIL"
    safe_reasoning = result["reasoning"].encode("ascii", errors="replace").decode("ascii")
    print(f"  [scorer] Verdict: {verdict} (score: {result['score']}) -- {safe_reasoning}")
    return result
