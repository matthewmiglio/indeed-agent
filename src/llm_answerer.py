"""LLM-based answering for custom screening questions on job applications.

When a form field doesn't match any known profile datum via keyword mapping,
this module sends the question + user profile + job context to Mistral to
generate an appropriate answer.
"""

import json
import time
import ollama

SYSTEM_PROMPT = """You ARE the candidate filling out a job application form yourself.
Answer in FIRST PERSON — say "I have…", "I worked…", "My experience…".
NEVER refer to the candidate in third person (no "Korrah has", no "the candidate",
no "as the applicant", no agent-speak). Write as if you are the person applying.

Rules:
- Be concise and professional, in first person
- For yes/no questions, answer ONLY "Yes" or "No"
- For short text questions, keep answers to 1-2 sentences
- For longer text fields (cover letter, "tell us about yourself", "comments", "why are you a good fit"), write 4-6 substantive sentences that tie your actual experience to the specific job — name skills from the posting, reference the company by name, and explain why you're motivated to apply
- For number fields, return ONLY the number
- Base all answers on the candidate's actual profile data
- NEVER fabricate experience or qualifications you don't have
- CRITICAL — DO NOT confuse the JOB POSTING with the candidate's resume.
  Phrases like "in my role as a [job title from posting]" are FORBIDDEN if
  that role does not appear in the candidate's profile. The job posting
  describes the job being APPLIED TO, not the candidate's history.
- CRITICAL — for "do you have experience with X" or "do you have [a credential / license / certification]" questions:
  - Answer "Yes" ONLY if the candidate's profile (skills, certifications,
    current/past job titles, experience_summary) EXPLICITLY contains X or
    an obvious synonym (e.g. "caregiving" ≈ "patient care").
  - Mere keyword overlap with the job description is NOT sufficient.
  - If you cannot point to a specific profile field that proves X, answer "No".
  - Certifications/licenses (e.g. "certified medical assistant", "CDL",
    "RN license") must be in the candidate's `certifications` list to answer Yes.
- If asked about a specific technology/skill not in your profile,
  answer "No" for yes/no questions, or mention related experience
  briefly (1 sentence) for free-form questions — do NOT claim mastery.
- If you truly cannot answer from the profile, respond with "SKIP"

Return ONLY valid JSON:
{
  "answer": "your answer here",
  "confidence": "high" | "medium" | "low",
  "reasoning": "brief explanation of your answer"
}
"""


def answer_question(question: str, field_type: str, options: list[str],
                    user_profile: dict, job_data: dict, model: str = "mistral") -> str | None:
    """Generate an answer for a custom screening question.

    Args:
        question: The label/question text of the form field.
        field_type: The HTML input type (text, textarea, select, radio, etc.).
        options: For select/radio fields, the available choices.
        user_profile: Full user profile dict.
        job_data: Current job posting data for context.
        model: Ollama model name.

    Returns:
        The answer string, or None if the LLM cannot answer (SKIP).
    """
    # Build a concise profile summary
    profile_parts = []
    for key in ["first_name", "last_name", "email", "phone", "city", "state",
                "current_job_title", "years_experience", "experience_summary",
                "highest_education", "degree_field", "skills", "certifications",
                "desired_salary", "work_type_preference"]:
        val = user_profile.get(key)
        if val is not None and val != "" and val != []:
            if isinstance(val, list):
                val = ", ".join(val)
            profile_parts.append(f"{key}: {val}")
    profile_text = "\n".join(profile_parts)

    options_text = ""
    if options:
        options_text = f"\nAvailable choices: {', '.join(options)}"
        options_text += "\nYou MUST pick exactly one of the above choices."

    prompt = f"""Candidate Profile:
{profile_text}

Job being applied to:
Title: {job_data.get('title', 'unknown')}
Company: {job_data.get('company', 'unknown')}
Description excerpt: {job_data.get('description', '')[:500]}

Form question: "{question}"
Field type: {field_type}{options_text}

Provide an appropriate answer for this form field."""

    safe_q = question[:80].encode("ascii", errors="replace").decode("ascii")
    print(f"  [llm] Answering: \"{safe_q}\"")
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
    print(f"  [llm] Responded in {elapsed:.1f}s ({tokens} tokens)")

    result = json.loads(raw)
    raw_answer = result.get("answer", "")
    # Mistral sometimes returns a list (e.g. ["Yes"]) or a dict; coerce to string.
    if isinstance(raw_answer, list):
        raw_answer = ", ".join(str(x) for x in raw_answer) if raw_answer else ""
    elif isinstance(raw_answer, dict):
        raw_answer = ", ".join(f"{k}: {v}" for k, v in raw_answer.items())
    elif raw_answer is None:
        raw_answer = ""
    answer = str(raw_answer).strip()
    confidence = result.get("confidence", "low")
    reasoning = result.get("reasoning", "")

    if answer.upper() == "SKIP" or not answer:
        print(f"  [llm] Cannot answer this question (confidence: {confidence})")
        return None

    safe_answer = answer[:100].encode("ascii", errors="replace").decode("ascii")
    print(f"  [llm] Answer: \"{safe_answer}\" (confidence: {confidence})")
    if reasoning:
        safe_reasoning = reasoning[:100].encode("ascii", errors="replace").decode("ascii")
        print(f"  [llm] Reasoning: {safe_reasoning}")

    return answer
