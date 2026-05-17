"""Natural-language prompt parser for job search intents.

Uses a local Ollama model to extract structured fields like job title,
location, quantity, job type, and preferences from free-form text.
"""

import json
import time
import ollama

SYSTEM_PROMPT = """You are a prompt parser for an Indeed job application agent.
Given a user's natural language request, extract a structured JSON intent.

Return ONLY valid JSON with these fields:
{
  "job_title": "what role to search for",
  "location": "city/state or 'remote' or null",
  "quantity": number (how many jobs to apply to, default 10),
  "job_type": "fulltime" | "parttime" | "contract" | "internship" | "any",
  "salary_min": number or null,
  "experience_level": "entry" | "mid" | "senior" | "any",
  "remote_preference": "remote" | "hybrid" | "onsite" | "any",
  "exclusions": "things to avoid (e.g. 'no startups', 'no contract agencies') or null",
  "resume_path": "specific resume path if mentioned, else null",
  "max_commute_minutes": "max commute time in minutes, or null. Convert distances: assume 30mph avg, so 30 miles = 60 min"
}

Examples:
User: "apply to 10 python developer jobs in new york"
{"job_title": "python developer", "location": "New York", "quantity": 10, "job_type": "any", "salary_min": null, "experience_level": "any", "remote_preference": "any", "exclusions": null, "resume_path": null}

User: "find 5 remote senior data engineer positions paying at least 150k, skip staffing agencies"
{"job_title": "senior data engineer", "location": "remote", "quantity": 5, "job_type": "fulltime", "salary_min": 150000, "experience_level": "senior", "remote_preference": "remote", "exclusions": "skip staffing agencies and recruiters", "resume_path": null}

User: "apply to 3 entry level marketing jobs in Chicago, part time"
{"job_title": "marketing", "location": "Chicago", "quantity": 3, "job_type": "parttime", "salary_min": null, "experience_level": "entry", "remote_preference": "any", "exclusions": null, "resume_path": null, "max_commute_minutes": null}

User: "search for python jobs within 20 minutes of me"
{"job_title": "python", "location": null, "quantity": 10, "job_type": "any", "salary_min": null, "experience_level": "any", "remote_preference": "any", "exclusions": null, "resume_path": null, "max_commute_minutes": 20}

User: "apply to 5 data analyst jobs within 30 miles of me"
{"job_title": "data analyst", "location": null, "quantity": 5, "job_type": "any", "salary_min": null, "experience_level": "any", "remote_preference": "any", "exclusions": null, "resume_path": null, "max_commute_minutes": 60}
"""


def parse_prompt(user_prompt: str, model: str = "mistral") -> dict:
    """Convert a free-form user request into a structured intent dict via Ollama.

    Args:
        user_prompt: Raw natural-language request from the user.
        model: Ollama model name to use for parsing.

    Returns:
        Dict with keys: job_title, location, quantity, job_type, salary_min,
        experience_level, remote_preference, exclusions, resume_path.
    """
    print(f"  [parser] Sending to {model}...")
    print(f"  [parser] Prompt: \"{user_prompt}\"")
    t0 = time.time()

    response = ollama.chat(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        format="json",
    )

    elapsed = time.time() - t0
    text = response["message"]["content"]
    tokens = response.get("eval_count", "?")
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
