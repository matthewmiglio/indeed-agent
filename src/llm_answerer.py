import re
"""LLM-based answering for custom screening questions on job applications.

When a form field doesn't match any known profile datum via keyword mapping,
this module sends the question + user profile + job context to Claude (Haiku)
to generate an appropriate answer.
"""

import json
import time
from claude_client import chat_json

SYSTEM_PROMPT = """You ARE the candidate, filling out this job application yourself. Write in FIRST PERSON — "I have…", "I worked…", "My experience…". Never refer to the candidate in third person and never use agent-speak ("the candidate", "the applicant", "as the AI"). You are the person applying.

Your only source of truth is the candidate profile provided in the user message. The job posting tells you what to tailor toward — it is NOT your history. This is the one rule that matters most: everything you claim must be grounded in a specific profile field.

Answer by field type:
- Yes/No question: reply exactly "Yes" or "No" — nothing else.
- Number field: reply with only the number.
- Multiple-choice (options given): reply with exactly one of the provided choices, verbatim.
- Short text: 1-2 concise, professional sentences.
- Long text (cover letter, "tell us about yourself", "why are you a good fit", comments): 4-6 substantive sentences that tie YOUR actual profile experience to THIS job — name relevant skills, reference the company by name, and state why you are motivated. When you write the company name, copy it EXACTLY as given in the Company field, character for character. Never correct, complete, expand, or "fix" an unusual or misspelled-looking company name (e.g. do not change "Brillian" to "Brilliant", or "Discernis" to "Discerns").

Truthfulness (do not violate):
- Never claim experience, skills, or credentials not in the profile. Do not inflate or round up.
- Never write "in my role as a <title from the posting>" unless that title is in your profile. The posting's title is the job you WANT, not one you held.
- "Do you have experience with X?" / "Do you hold <license/certification>?": answer "Yes" ONLY if a profile field (skills, certifications, current/past job titles, experience_summary) explicitly contains X or an obvious synonym (e.g. "caregiving" ≈ "patient care"). Keyword overlap with the job description does NOT count. Licenses/certifications must appear in the profile's certifications list. If you can't point to the field, answer "No".
- Asked about a skill/technology you don't have: "No" for yes/no; for free-form, note related experience in one sentence without claiming mastery.
- "How many years of <skill/domain> experience do you have?": answer a positive number ONLY if that exact skill/domain (or an obvious synonym) is in a profile field. If it is, give your real years with it, never more than your total years of experience. If the skill is NOT in the profile, answer "0" — do not default to your total experience for a skill you can't point to (keyword overlap with the posting does not count). Honest "0" is correct even when it hurts the application.
- If the profile gives you nothing to answer from, reply with exactly "SKIP".

Respect any character limit stated in the question; stay well under it. Finish your last sentence before the limit rather than running up against it.

NEVER use em dashes or en dashes (the "—" or "–" characters). Use a period, comma, parentheses, colon, or semicolon instead. This is a hard rule for every answer.

Return ONLY valid JSON:
{
  "answer": "your answer here",
  "confidence": "high" | "medium" | "low",
  "reasoning": "which profile field(s) you based this on"
}
"""


def answer_question(question: str, field_type: str, options: list[str],
                    user_profile: dict, job_data: dict, model: str = "claude-haiku-4-5",
                    max_length: int = 0) -> str | None:
    """Generate an answer for a custom screening question.

    Args:
        question: The label/question text of the form field.
        field_type: The HTML input type (text, textarea, select, radio, etc.).
        options: For select/radio fields, the available choices.
        user_profile: Full user profile dict.
        job_data: Current job posting data for context.
        model: Claude model ID.

    Returns:
        The answer string, or None if the LLM cannot answer (SKIP).
    """
    # Note: experience-threshold questions ("minimum of N years…") are handled
    # deterministically in form_filler.map_field_to_profile, which runs before
    # this and also covers the pre-fill override path.

    # Build a concise profile summary
    profile_parts = []
    for key in ["first_name", "last_name", "email", "phone", "city", "state",
                "current_job_title", "current_employer", "current_job_start_date",
                "years_experience", "experience_summary",
                "highest_education", "degree_field", "school_name", "graduation_year",
                "skills", "certifications",
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

    length_text = ""
    if max_length and max_length > 0:
        # Aim for ~70% of the limit to leave headroom for variation; never go over.
        target = int(max_length * 0.7)
        length_text = (
            f"\n\nIMPORTANT: This field has a HARD character limit of {max_length} characters. "
            f"Your answer MUST be {target} characters or fewer. "
            f"Count carefully and prioritize the most relevant sentences if you need to cut."
        )

    prompt = f"""Candidate Profile:
{profile_text}

Job being applied to:
Title: {job_data.get('title', 'unknown')}
Company: {job_data.get('company', 'unknown')}
Description excerpt: {job_data.get('description', '')[:500]}

Form question: "{question}"
Field type: {field_type}{options_text}{length_text}

Provide an appropriate answer for this form field."""

    safe_q = question[:80].encode("ascii", errors="replace").decode("ascii")
    print(f"  [llm] Answering: \"{safe_q}\"")
    t0 = time.time()

    # The model (or CLI transport) occasionally returns an empty string or a
    # non-JSON blob. Retry once, then give up gracefully — a single bad reply
    # must not crash the whole application run. Returning None leaves the field
    # for the caller to report as unhandled.
    result = None
    for attempt in range(2):
        raw, tokens = chat_json(SYSTEM_PROMPT, prompt, model=model)
        print(f"  [llm] Responded in {time.time() - t0:.1f}s ({tokens} tokens)")
        try:
            result = json.loads(raw)
            break
        except (json.JSONDecodeError, TypeError):
            print(f"  [llm] non-JSON/empty reply (attempt {attempt + 1}/2): {str(raw)[:60]!r}")
    if result is None:
        print("  [llm] giving up on this question after 2 bad replies")
        return None
    raw_answer = result.get("answer", "")
    # The model sometimes returns a list (e.g. ["Yes"]) or a dict; coerce to string.
    if isinstance(raw_answer, list):
        raw_answer = ", ".join(str(x) for x in raw_answer) if raw_answer else ""
    elif isinstance(raw_answer, dict):
        raw_answer = ", ".join(f"{k}: {v}" for k, v in raw_answer.items())
    elif raw_answer is None:
        raw_answer = ""
    answer = str(raw_answer).strip()

    # Drafts sent to employers: never em/en dashes (user rule). The model obeys
    # the prompt only intermittently, so strip deterministically.
    answer = answer.replace(" — ", ", ").replace("—", ", ")
    answer = answer.replace(" – ", ", ").replace("–", "-").replace(" ,", ",")

    # Hard-respect the char limit, but cut at a sentence boundary so a trimmed
    # answer still reads cleanly instead of stopping mid-word.
    if max_length and len(answer) > max_length:
        cut = answer[:max_length]
        end = max(cut.rfind(". "), cut.rfind(".\n"), cut.rfind("! "), cut.rfind("? "))
        answer = cut[:end + 1] if end > max_length * 0.5 else cut.rstrip()

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
