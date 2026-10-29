"""Natural-language prompt parser for job search intents.

Uses Claude (Haiku) to extract structured fields like job title,
location, quantity, job type, and preferences from free-form text.
"""

import json
import time
from claude_client import chat_json

SYSTEM_PROMPT = """You extract a structured search intent from a user's natural-language request to an Indeed job-application agent. Your JSON is consumed directly by code, so it must be exact.

Output ONE JSON object with ALL of these keys, every time:
{
  "job_title": string,               // role to search; if unstated, "software engineer"
  "location": string | null,          // "City, ST" or "remote"; null if unstated
  "quantity": number,                 // jobs to apply to; default 10
  "job_type": "fulltime" | "parttime" | "contract" | "internship" | "any",
  "salary_min": number | null,        // annual USD floor (150k -> 150000); null if unstated
  "experience_level": "entry" | "mid" | "senior" | "any",
  "remote_preference": "remote" | "hybrid" | "onsite" | "any",
  "exclusions": string | null,        // what to avoid; null if none
  "resume_path": string | null,       // explicit file path if the user names one; else null
  "max_commute_minutes": number | null
}

Rules:
- Extract only what the user states or clearly implies. Never invent a salary, location, quantity, or exclusion that was not given.
- Map wording to enums: "part time" -> parttime, "full time" -> fulltime, "contract"/"contractor" -> contract, "intern"/"internship" -> internship; otherwise "any". Same for experience_level ("junior"/"entry level" -> entry) and remote_preference.
- If the user says "remote", set BOTH location "remote" and remote_preference "remote".
- max_commute_minutes: minutes given -> use as-is. Distance given -> convert at 30 mph (minutes = miles * 2, so 30 miles = 60). "near me"/"close by" with no number -> null.
- Normalize any recruiter/staffing-agency avoidance to exactly "skip staffing agencies and recruiters".

Return ONLY the JSON object. No prose, no markdown fences.

Examples:
User: "apply to 10 python developer jobs in new york"
{"job_title": "python developer", "location": "New York, NY", "quantity": 10, "job_type": "any", "salary_min": null, "experience_level": "any", "remote_preference": "any", "exclusions": null, "resume_path": null, "max_commute_minutes": null}

User: "find 5 remote senior data engineer positions paying at least 150k, skip staffing agencies"
{"job_title": "senior data engineer", "location": "remote", "quantity": 5, "job_type": "fulltime", "salary_min": 150000, "experience_level": "senior", "remote_preference": "remote", "exclusions": "skip staffing agencies and recruiters", "resume_path": null, "max_commute_minutes": null}

User: "apply to 3 entry level marketing jobs in Chicago, part time"
{"job_title": "marketing", "location": "Chicago, IL", "quantity": 3, "job_type": "parttime", "salary_min": null, "experience_level": "entry", "remote_preference": "any", "exclusions": null, "resume_path": null, "max_commute_minutes": null}

User: "search for python jobs within 20 minutes of me"
{"job_title": "python", "location": null, "quantity": 10, "job_type": "any", "salary_min": null, "experience_level": "any", "remote_preference": "any", "exclusions": null, "resume_path": null, "max_commute_minutes": 20}

User: "apply to 5 data analyst jobs within 30 miles of me"
{"job_title": "data analyst", "location": null, "quantity": 5, "job_type": "any", "salary_min": null, "experience_level": "any", "remote_preference": "any", "exclusions": null, "resume_path": null, "max_commute_minutes": 60}
"""


def parse_prompt(user_prompt: str, model: str = "claude-haiku-4-5") -> dict:
    """Convert a free-form user request into a structured intent dict via Claude.

    Args:
        user_prompt: Raw natural-language request from the user.
        model: Claude model ID to use for parsing.

    Returns:
        Dict with keys: job_title, location, quantity, job_type, salary_min,
        experience_level, remote_preference, exclusions, resume_path.
    """
    print(f"  [parser] Sending to {model}...")
    print(f"  [parser] Prompt: \"{user_prompt}\"")
    t0 = time.time()

    text, tokens = chat_json(SYSTEM_PROMPT, user_prompt, model=model)

    elapsed = time.time() - t0
    print(f"  [parser] Model responded in {elapsed:.1f}s ({tokens} tokens)")
    print(f"  [parser] Raw output: {text}")

    intent = json.loads(text)

    # Apply defaults
    intent.setdefault("job_title", "software engineer")
    intent.setdefault("location", None)
    intent.setdefault("quantity", 10)
    intent.setdefault("job_type", "any")
    intent.setdefault("salary_min", None)
    intent.setdefault("experience_level", "any")
    intent.setdefault("remote_preference", "any")
    intent.setdefault("exclusions", None)
    intent.setdefault("resume_path", None)
    intent.setdefault("max_commute_minutes", None)

    print(f"  [parser] Parsed intent: {json.dumps(intent, indent=2)}")
    return intent
