"""Job posting scorer that evaluates how well a listing matches the candidate.

Sends job data plus the user's profile and search criteria to a local Ollama
model, which returns a 0-100 score and a pass/fail verdict.
"""

import json
import time
import ollama

SYSTEM_PROMPT = """You are a job match evaluator for an Indeed job application agent.
Given a job posting and a candidate's profile, score the match from 0 to 100
and decide if the candidate should apply.

Return ONLY valid JSON:
{
  "score": number 0-100,
  "pass": true/false,
  "reasoning": "brief explanation"
}

HARD RULES — these always result in pass: false:
- If the job requires skills the candidate clearly does not have and cannot learn quickly, FAIL.
- If the job is in a completely different field from the candidate's experience, FAIL.
- If salary is listed and is significantly below the candidate's minimum, FAIL.
- If the candidate specified exclusions and the job matches an exclusion, FAIL.
- If the job requires a specific license/certification the candidate doesn't have, FAIL.

Scoring guidelines (for listings that pass hard rules):
- Strong skill match = higher score
- Matching experience level = higher score
- Location/remote preference match = higher score
- Good salary relative to expectations = higher score
- Well-known or reputable company = slight boost
- Vague or suspiciously generic posting = lower score
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


def score_job(job_data: dict, profile: dict, criteria: dict, model: str = "mistral") -> dict:
    """Score a job posting against the candidate's profile and search criteria.

    Args:
        job_data: Extracted job details (title, company, description, etc.).
        profile: User profile dict from profile_manager.
        criteria: Parsed intent dict from the prompt parser.
        model: Ollama model name for evaluation.

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

    response = ollama.chat(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        format="json",
    )

    elapsed = time.time() - t0
    raw = response["message"]["content"]
    tokens = response.get("eval_count", "?")
    safe_raw = raw.encode("ascii", errors="replace").decode("ascii")
    print(f"  [scorer] Model responded in {elapsed:.1f}s ({tokens} tokens)")
    print(f"  [scorer] Raw output: {safe_raw}")

    result = json.loads(raw)
    result.setdefault("score", 50)
    result.setdefault("pass", True)
    result.setdefault("reasoning", "")

    verdict = "PASS" if result["pass"] else "FAIL"
    safe_reasoning = result["reasoning"].encode("ascii", errors="replace").decode("ascii")
    print(f"  [scorer] Verdict: {verdict} (score: {result['score']}) -- {safe_reasoning}")
    return result
