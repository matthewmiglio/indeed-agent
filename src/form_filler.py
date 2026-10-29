"""Indeed Easy Apply form automation.

Handles the entire multi-step Easy Apply flow: clicking the Apply button,
filling form fields using profile data (keyword matching) or LLM fallback,
uploading resume, and submitting the application.
"""

import asyncio
from playwright.async_api import Page
from browser import human_delay, dump_page
from form_analyzer import FormField, analyze_form_page, is_review_page, is_confirmation_page, detect_already_applied
from llm_answerer import answer_question
import field_inventory
import qa_logger

# Dry-run mode: walk the entire flow but never click the final Submit button.
# Toggled by main.py via --dry-run.
DRY_RUN = False

# Keyword mapping: if the label (lowercased) contains the key, use the profile value.
# Ordered from most specific to least specific to avoid false matches.
FIELD_MAP = [
    # Resume picker — handled by select_upload_resume_option(); skip per-field fill
    # so we don't click a hidden Indeed-Resume radio with a 30s timeout.
    ("use your indeed resume", "_skip"),
    ("indeed resume", "_skip"),
    ("ai-tailored", "_skip"),

    # SMS / text-message opt-in (form variants).
    ("i would like to receive text", "_yes"),
    ("receive text messages", "_yes"),
    ("text notification preferences", "_yes"),

    # Static answers (user-mandated) — must come first so they win over generic matches.
    ("current employee", "_no"),
    ("currently employed by", "_no"),
    ("current or former employee", "_no"),
    ("former employee", "_no"),
    ("referred by", "_no"),
    ("employee referral", "_no"),
    ("referral from", "_no"),
    ("were you referred", "_no"),
    ("consent to receive", "_yes"),
    ("informational recruiting", "_yes"),
    # Commute — always Yes. Saying No is an instant auto-decline on most
    # applications, and the candidate is willing to drive ~30-35min anyway.
    ("commute", "_yes"),
    ("able to commute", "_yes"),
    ("reliably commute", "_yes"),
    ("travel to", "_yes"),
    ("text messages from", "_yes"),
    ("sms terms", "_yes"),
    ("receive informational", "_yes"),
    ("consent to receiving text", "_yes"),
    # Don't let Indeed persist agent-generated answers to the account (they get
    # re-served as pre-fills on later jobs and can ride along wrong). Always No.
    ("save my answers", "_no"),
    ("save these answers", "_no"),
    ("share these answers", "_no"),
    ("share my answers", "_no"),

    # Work authorization & sponsorship — match BEFORE the generic "state" pattern
    # because labels often contain "United States".
    ("authorized to work", "work_authorized_us"),
    ("legally authorized", "work_authorized_us"),
    ("legally able to work", "work_authorized_us"),
    ("work authorization", "work_authorized_us"),
    ("right to work", "work_authorized_us"),
    ("eligible to work", "work_authorized_us"),
    ("require sponsorship", "sponsorship_needed"),
    ("visa sponsorship", "sponsorship_needed"),
    ("immigration sponsorship", "sponsorship_needed"),
    ("need sponsorship", "sponsorship_needed"),

    # Identity & contact — narrower patterns first to avoid false matches.
    # "first and last name" must precede "last name" (which it contains as a
    # substring) or it would map to last_name only.
    ("first and last name", "_full_name"),
    ("first & last name", "_full_name"),
    ("name (first and last", "_full_name"),
    ("first name", "first_name"),
    ("last name", "last_name"),
    ("family name", "last_name"),
    ("given name", "first_name"),
    ("full name", "_full_name"),          # special: first + last
    ("email address", "email"),
    ("your email", "email"),
    ("phone number", "phone"),
    ("mobile number", "phone"),
    ("telephone number", "phone"),
    ("contact number", "phone"),
    ("your phone", "phone"),
    ("your mobile", "phone"),
    ("your city", "city"),
    ("city of residence", "city"),
    ("city only", "city"),
    ("location (city", "city"),
    ("your location", "city"),
    ("your state", "state"),
    ("state of residence", "state"),
    ("state/province", "state"),
    ("province", "state"),
    ("zip code", "zip_code"),
    ("postal code", "zip_code"),
    ("zip/postal", "zip_code"),
    ("linkedin", "linkedin_url"),
    ("linked in", "linkedin_url"),   # spaced variant; otherwise the LLM guesses the handle
    ("how did you hear", "_how_heard"),   # truthful: we found the job on Indeed

    # Address — line-2 / apt / suite are deliberately left blank; line 1 gets
    # the STREET only (city/state/zip have their own fields). Line-2 patterns
    # come first so they win the substring match over the generic "address".
    ("address line 2", "_blank"),
    ("address 2", "_blank"),
    ("apartment", "_blank"),
    ("apt", "_blank"),
    ("suite", "_blank"),
    ("unit number", "_blank"),
    ("address line 1", "street_address"),
    ("street address", "street_address"),
    ("address 1", "street_address"),
    ("mailing address", "street_address"),
    ("home address", "home_address"),
    ("current address", "street_address"),
    ("street", "street_address"),
    ("address", "street_address"),

    ("country", "_us"),
    ("your name", "_full_name"),
    ("legal name", "_full_name"),
    ("today's date", "_today"),
    ("today’s date", "_today"),  # unicode right-single-quote variant
    ("current date", "_today"),
    ("date signed", "_today"),
    ("signature date", "_today"),

    # Demographics — most ask voluntary-disclosure questions with standard options.
    ("protected veteran", "_not_veteran"),
    ("veteran status", "_not_veteran"),
    ("disability", "_no_disability"),  # catches "Voluntary Self-Identification of Disability" etc.
    ("privacy policy", "_agree"),
    ("i have read and agree", "_agree"),
    ("acknowledge", "_agree"),

    # Gender / ethnicity — profile values come from yaml (may be blank — LLM still
    # handles those cases). Patterns are ordered before the generic "race" miss.
    ("gender", "gender"),
    ("pronoun", "pronouns"),
    ("ethnicity", "ethnicity"),
    ("race/ethnicity", "ethnicity"),
    ("race", "ethnicity"),
    ("hispanic or latino", "hispanic_latino"),
    ("date of birth", "date_of_birth"),
    ("birth date", "date_of_birth"),
    ("hourly rate", "desired_hourly_rate"),
    ("hourly pay", "desired_hourly_rate"),
    ("availability", "availability"),
    ("days of the week", "availability"),
    ("days and times", "availability"),

    # Experience
    ("years of experience", "years_experience"),
    ("years experience", "years_experience"),
    ("how many years", "years_experience"),
    ("current job title", "current_job_title"),
    ("current title", "current_job_title"),
    ("current employer", "current_employer"),
    ("current company", "current_employer"),
    # Employment-history block fields (Indeed asks these on some forms).
    ("employer name", "current_employer"),
    ("company name", "current_employer"),
    ("most recent employer", "current_employer"),
    ("most recent company", "current_employer"),
    ("recent company", "current_employer"),
    ("recent employer", "current_employer"),
    ("previous employer", "current_employer"),
    # Employer location (employment-history blocks). Specific enough not to
    # collide with the candidate's own "your city"/"your state" patterns.
    ("employer city", "current_employer_city"),
    ("company city", "current_employer_city"),
    ("employer state", "current_employer_state"),
    ("company state", "current_employer_state"),
    ("recent job title", "current_job_title"),
    ("most recent job title", "current_job_title"),
    ("previous job title", "current_job_title"),
    ("recent position", "current_job_title"),
    # Employment-history dates — fall through to None (LLM) when profile
    # lacks the data; we don't want to auto-fill today's date as a past
    # employment start date.
    ("start year", "_start_year"),
    ("start date", "current_job_start_date"),
    ("end year", "_blank"),      # current role -> no end year
    ("end date", "_end_date"),  # special — handled below

    # Education
    ("highest level of education", "highest_education"),
    ("education level", "highest_education"),
    ("highest degree", "highest_education"),
    # School + graduation year — we have these in the profile; they must be
    # mapped or they fall to the LLM (which isn't given them) and come out blank.
    ("graduation year", "graduation_year"),
    ("grad year", "graduation_year"),
    ("year of graduation", "graduation_year"),
    ("year graduated", "graduation_year"),
    ("most recent school", "school_name"),
    ("education institution", "school_name"),
    ("school name", "school_name"),
    ("name of school", "school_name"),
    ("name of institution", "school_name"),
    ("university name", "school_name"),
    ("college name", "school_name"),
    ("school/university", "school_name"),
    ("institution name", "school_name"),
    ("gpa", "_blank"),                 # not tracked — leave blank by request
    ("grade point average", "_blank"),
    ("degree", "degree_field"),

    # Salary
    ("salary", "desired_salary"),
    ("desired pay", "desired_salary"),
    ("expected compensation", "desired_salary"),
    ("pay expectation", "desired_salary"),

    # Start date — only patterns clearly meaning "when can you begin work"
    # (NOT employment-history "Start Date" which needs an actual past date).
    ("when can you start", "start_date"),
    ("available to start", "start_date"),
    ("earliest start", "start_date"),
    ("when are you available", "start_date"),

    # Relocation & commute — always Yes. Saying No to either is an instant
    # auto-decline on most applications, regardless of the profile flag.
    ("relocat", "_yes"),
    ("willing to move", "_yes"),
    ("commute", "_yes"),

    # Common screening
    ("driver", "has_drivers_license"),
    ("transportation", "has_reliable_transportation"),
    ("background check", "can_pass_background_check"),
    ("drug test", "can_pass_drug_test"),
    ("drug screen", "can_pass_drug_test"),
    ("18 years", "is_18_or_older"),
    ("at least 18", "is_18_or_older"),
]


def _extract_salary_range(job_data: dict) -> tuple[float, float] | None:
    """Pull a (low, high) annual-salary range from the job posting if present.

    Looks at job_data['salary'] first (Indeed's structured field), then falls
    back to scanning the description for a $X-$Y pattern. Normalizes hourly
    rates to annual (×2080) and 'K' shorthand. Returns None if no range found.
    """
    import re
    candidates = []
    for src in (job_data.get("salary") or "", job_data.get("description") or ""):
        if not src:
            continue
        # Match $1,200 - $2,500 / $1.2k-$2.5k / $50,000 to $70,000 / $25-$35 an hour
        for m in re.finditer(
            r"\$\s*([\d,]+(?:\.\d+)?)\s*([kK])?\s*(?:-|to|–)\s*\$?\s*([\d,]+(?:\.\d+)?)\s*([kK])?(\s*(?:an?\s*hour|/\s*hr|per\s*hour|hourly))?",
            src,
        ):
            lo_str, lo_k, hi_str, hi_k, hourly = m.groups()
            try:
                lo = float(lo_str.replace(",", ""))
                hi = float(hi_str.replace(",", ""))
            except ValueError:
                continue
            if lo_k:
                lo *= 1000
            if hi_k:
                hi *= 1000
            if hourly:
                lo *= 2080
                hi *= 2080
            if lo > 0 and hi >= lo:
                candidates.append((lo, hi))
            break  # first hit in each source wins
        if candidates:
            break
    if not candidates:
        return None
    return candidates[0]


def _salary_answer(job_data: dict, profile: dict, is_hourly: bool = False) -> str | None:
    """Compute the salary answer.

    Returns the bottom-20% of the job's posted range when present; otherwise
    falls back to ``profile['desired_salary']`` (annual) or
    ``profile['desired_hourly_rate']`` (hourly).

    If ``is_hourly`` is True the result is normalized to a per-hour figure
    (annual is divided by 2080). This keeps "what's your hourly rate?"
    questions from being answered with an annual salary.
    """
    rng = _extract_salary_range(job_data) if job_data else None
    if rng:
        lo, hi = rng
        target = lo + (hi - lo) * 0.20
        if is_hourly and target > 200:
            # The posted range is annual; convert to hourly.
            target = target / 2080
        return str(int(round(target)))

    if is_hourly:
        hourly = profile.get("desired_hourly_rate")
        if hourly:
            return str(hourly)
        annual = profile.get("desired_salary")
        if annual:
            try:
                return str(int(round(float(annual) / 2080)))
            except (TypeError, ValueError):
                return str(annual)
        return None
    return profile.get("desired_salary")


def _has_credential(profile: dict, keyword: str) -> bool:
    """True iff `keyword` (or a clear synonym) appears in the candidate's
    certifications list."""
    certs = profile.get("certifications") or []
    if isinstance(certs, str):
        certs = [certs]
    kw = (keyword or "").strip().lower()
    if not kw:
        return False
    blob = " | ".join(str(c).lower() for c in certs)
    return kw in blob


def _credential_answer(label: str, profile: dict) -> str | None:
    """If the label asks "do you have a [valid/current/active] [X]
    certification/license/credential/cert", look up profile.certifications.
    Return 'Yes' / 'No' if recognized; None to fall through."""
    import re
    m = re.search(
        r"(?:do you|have you|are you)[^?]*?\b(?:have|hold|possess|maintain|carry)\b"
        r"[^?]*?\b(?:a|an|your|the|valid|current|active|required|proper)?\s*"
        r"(.{2,40}?)\s*(?:certification|certificate|certified|license|licens(?:ed|ure)|credential|registration|registered)\b",
        label.lower(),
    )
    if not m:
        # Also catch "Tips Certification?", "CPR Certified?" patterns
        m2 = re.search(
            r"\b(?:valid|current|active)?\s*(.{2,40}?)\s*(?:certification|license|credential)\b",
            label.lower(),
        )
        if not m2:
            return None
        keyword = m2.group(1).strip()
    else:
        keyword = m.group(1).strip()
    # Trim filler words
    keyword = re.sub(r"\b(a|an|the|your|valid|current|active|of|in)\b", "", keyword).strip()
    if not keyword or len(keyword) < 2:
        return None
    return "Yes" if _has_credential(profile, keyword) else "No"


def _experience_with_answer(label: str, profile: dict) -> str | None:
    """If the label asks "do you have experience with [X]", check whether X
    appears in skills / job titles / summary / degree. Return Yes when found,
    No otherwise. None when this isn't an experience question.

    This catches the LLM-overclaim failure mode where the model would say Yes
    to anything that sounds reasonable for the job posting (e.g. "Food safety"
    on a barista app even though the candidate has no food-service history).
    """
    import re
    # Match both "experience with X" and "experience working in X" / "experience in X"
    # forms — Indeed employers phrase the same question multiple ways.
    m = re.search(
        r"(?:do you|have you)[^?]*?\b(?:have|had)\b[^?]*?"
        r"\b(?:experience|exposure|familiarity|working knowledge)\b[^?]*?"
        r"(?:working\s+(?:in|at|on|as)\s+|with\s+|in\s+(?:a|an|the\s+)?|using\s+|in\s+)"
        r"(.{2,80}?)(?:\?|\.|\*|$)",
        label.lower(),
    )
    if not m:
        return None
    topic = re.sub(r"\b(a|an|the|using|in|of)\b", "", m.group(1)).strip()
    if len(topic) < 2:
        return None
    haystack = " ".join(str(v).lower() for v in (
        " ".join(profile.get("skills") or []),
        profile.get("current_job_title") or "",
        profile.get("experience_summary") or "",
        profile.get("degree_field") or "",
        profile.get("current_employer") or "",
    ))
    # AI/ML questions: phrasings like "AI models", "ML frameworks", "deep
    # learning", "LLMs", "generative AI" rarely match a literal skill token,
    # and "ai"/"ml" are too short for the word filter below — yet the candidate
    # clearly has this background (PyTorch, ML, computer vision, LLM/RAG work).
    # Answer Yes when the question is AI/ML-flavored AND the profile shows a
    # core ML/AI signal. Gated on real signal, so it never blanket-claims AI.
    tl = " " + topic.lower() + " "
    ai_ml_question = any(t in tl for t in (
        " ai ", "ai model", "a.i", "artificial intelligence", "machine learning",
        " ml ", "ml model", "ml framework", "deep learning", "neural network",
        "llm", "large language model", "generative ai", "gen ai", "genai",
        "computer vision",
    ))
    if ai_ml_question and any(s in haystack for s in (
        "machine learning", "pytorch", "tensorflow", "computer vision",
        "deep learning", "neural", "reinforcement learning", "llm",
        "artificial intelligence",
    )):
        return "Yes"
    # Filter out generic vocabulary that's almost always present in any
    # experience_summary, e.g. "Do you have experience with Accounting
    # and finance experience?" must not match just because the summary
    # contains the word "experience".
    STOPWORDS = {
        "experience", "exp", "background", "skills", "skill", "knowledge",
        "familiarity", "exposure", "history", "training", "expertise",
        "abilities", "ability", "work", "working", "use", "using",
        "and", "or", "with", "in", "of", "the", "a", "an", "any", "some",
    }
    topic_words = [
        w for w in re.split(r"\W+", topic.lower())
        if len(w) >= 3 and w not in STOPWORDS
    ]
    if not topic_words:
        return None
    # Require the FIRST significant word (typically the most specific noun
    # in the phrase — e.g. "aerospace" in "aerospace / defense / AS9100
    # quality environment") to appear in the profile, OR all topic words
    # to appear. This keeps generic words like "quality" from triggering
    # a false Yes when the user has "Quality Control" in skills but no
    # actual aerospace experience.
    head = topic_words[0]
    if head in haystack:
        return "Yes"
    if len(topic_words) > 1 and all(w in haystack for w in topic_words):
        return "Yes"
    return "No"


def _job_specific_years(label: str, profile: dict) -> str | None:
    """If the label asks "how many years of [X] experience", check whether X
    matches the candidate's skills / job titles / summary. Return the
    profile's years_experience when it does, "0" otherwise."""
    import re
    m = re.search(r"(?:how many )?years\s+(?:of\s+)?(.{2,40}?)\s+(?:experience|exp\.?|background)\b",
                  label.lower())
    if not m:
        return None
    topic = re.sub(r"\b(of|in|with|as|a|an|the)\b", "", m.group(1)).strip()
    if not topic or len(topic) < 2:
        return None
    haystack = " ".join(str(v).lower() for v in (
        " ".join(profile.get("skills") or []),
        profile.get("current_job_title") or "",
        profile.get("experience_summary") or "",
        profile.get("degree_field") or "",
        profile.get("current_employer") or "",
    ))
    # Word-level overlap: at least one significant word from `topic` must appear.
    topic_words = [w for w in re.split(r"\W+", topic) if len(w) >= 3]
    matched = any(w in haystack for w in topic_words)
    if matched:
        return str(profile.get("years_experience") or 0)
    return "0"


def _shift_answer(label: str, profile: dict) -> str | None:
    """Map day/night/overnight/morning shift checkboxes against the
    candidate's stated availability."""
    avail = (profile.get("availability") or "").lower()
    if not avail:
        return None
    label_lc = label.lower()
    # Default availability heuristics from the profile string
    is_day = any(k in avail for k in ("8am", "9am", "7am", "morning", "day", "m-f"))
    is_night = any(k in avail for k in ("night", "evening", "pm-", "5pm-", "6pm-"))
    is_overnight = "overnight" in avail or "graveyard" in avail
    is_weekend = "weekend" in avail or "saturday" in avail or "sunday" in avail
    is_holiday = "holiday" in avail
    if "overnight" in label_lc or "graveyard" in label_lc:
        return "Yes" if is_overnight else "No"
    if "night" in label_lc or "evening" in label_lc:
        return "Yes" if is_night else "No"
    if "morning" in label_lc or "day shift" in label_lc or "afternoon" in label_lc:
        return "Yes" if is_day else "No"
    if "weekend" in label_lc:
        return "Yes" if is_weekend else "No"
    if "holiday" in label_lc:
        return "Yes" if is_holiday else "No"
    return None


def map_field_to_profile(field: FormField, profile: dict, job_data: dict | None = None) -> str | None:
    """Try to map a form field to a profile value using keyword matching.

    Returns the string value to fill, or None if no match found.
    """
    label = field.label_text.lower()

    # Experience-threshold screening ("Do you have a minimum of N years…") on a
    # Yes/No field. Deterministic so it also fires on the pre-fill override path
    # (the LLM tends to answer "Yes" regardless — a fabrication when we have
    # fewer years). Compare N to years_experience and answer truthfully.
    yrs = profile.get("years_experience")
    if yrs is not None and field.field_type in ("radio", "select", "checkbox"):
        import re as _re
        m = _re.search(r'(\d+)\s*\+?\s*years', label)
        if m and any(w in label for w in ("minimum", "at least", "or more",
                                          "+ years", "do you have", "more than")):
            try:
                return "Yes" if int(yrs) >= int(m.group(1)) else "No"
            except ValueError:
                pass

    # Start-date fields that require a real MM/DD/YYYY value. The profile's
    # ``start_date`` is often "Immediately" / "ASAP" which fails Indeed's date
    # validator. If the field is recognizable as a date input (label contains
    # "date" or field type is date), substitute today's date in MM/DD/YYYY.
    # Availability "when can you start" date. Bare "Start Date" is excluded
    # because it's often part of an employment-history block (past job start),
    # in which case today's date would be wrong.
    if ("earliest start" in label or "available to start" in label
            or "when can you start" in label or "desired start" in label
            or "anticipated start" in label or "soonest start" in label
            or ("start date" in label and any(
                k in label for k in ("avail", "can you", "earliest", "soonest", "would you")
            ))):
        from datetime import date
        return date.today().strftime("%m/%d/%Y")

    # Bare "Date *" / "Today's Date *" — the signature-date field on demographic
    # / EEO pages. Without this catch the LLM hallucinates dates like 1999-01-01.
    # Match when the label is just "date" (with optional asterisk / whitespace).
    stripped = label.strip().rstrip("*").rstrip(":").strip().strip(" ")
    if stripped in ("date", "today's date", "today’s date", "current date",
                    "signature date", "date signed", "date *"):
        from datetime import date
        return date.today().strftime("%m/%d/%Y")

    # Bug T — Country select disguised in a phone-number label.
    # Forms like "Mobile Number...Country *" are SELECTS of country names. If
    # the field is a select whose options contain country signals, pick US
    # FIRST — before the phone-keyword match wins.
    if field.field_type == "select":
        opts_blob = " | ".join(field.options).lower()
        country_signals = ("united states", "canada", "united kingdom", "australia")
        # Look for the +country-code shape too: "United States (+1)", "Canada (+1)"
        looks_country = (
            (sum(1 for c in country_signals if c in opts_blob) >= 2)
            or "(+1)" in opts_blob
        )
        if looks_country and ("country" in label or "country code" in label or "phone" in label):
            return "United States"

    # State/Province fields that are SECRETLY country dropdowns. Some forms
    # mislabel the field — e.g. a "State/Province *" select whose only options
    # are "United States" and "Canada". When we detect that shape, answer with
    # the country instead of the state code, otherwise the field stays empty
    # and the form refuses to advance.
    if "state" in label or "province" in label:
        opts_blob = " | ".join(field.options).lower()
        country_signals = ("united states", "canada", "united kingdom", "australia")
        if any(c in opts_blob for c in country_signals) and "michigan" not in opts_blob:
            return "United States"

    # Sponsorship questions win over work-authorization wording. Some labels read
    # "require sponsorship or support for continued work authorization", which would
    # otherwise match the work_authorized_us mapping ("work authorization" -> Yes)
    # and give the OPPOSITE, application-sinking answer. If the label mentions
    # sponsorship, answer from sponsorship_needed.
    if "sponsor" in label:
        need = profile.get("sponsorship_needed")
        if need is not None:
            return "Yes" if need else "No"

    # Veteran self-ID. Phrasing varies ("I am not a protected veteran" vs Infosys's
    # "No, I am not a veteran under one of the classifications"), so pick the actual
    # option that means "not a veteran" rather than returning a fixed string.
    if "veteran" in label and field.field_type in ("radio", "select", "checkbox") and field.options:
        for pat in ("no, i am not", "not a protected veteran", "i am not a veteran",
                    "not a veteran", "prefer not", "wish to answer"):
            for o in field.options:
                if pat in o.lower():
                    return o
        for o in field.options:
            if o.strip().lower().startswith("no"):
                return o

    # "Are you Hispanic or Latino?" is a Yes/No question; must beat the generic
    # "race"/"ethnicity" patterns (the prompt often ends "...regardless of race").
    if "hispanic" in label and field.field_type in ("radio", "select", "checkbox"):
        hl = profile.get("hispanic_latino")
        if hl is not None:
            return "Yes" if hl else "No"

    # Eligibility Yes/No screens ("Do you have a minimum of a Bachelor's degree",
    # "at least the minimum years of relevant experience"). These are radios/selects,
    # so returning a degree field or a years number leaves them unselected. Answer
    # truthfully from the profile. Gated on "minimum"/"at least" so it never catches
    # skill-specific "do you have experience with X" questions (those go to
    # _experience_with_answer / the LLM).
    if field.field_type in ("radio", "select", "checkbox") and (
            "minimum" in label or "at least" in label or "meet the" in label):
        if any(k in label for k in ("degree", "bachelor", "master", "diploma")):
            edu = (profile.get("highest_education") or "").lower()
            has_degree = any(k in edu for k in (
                "bachelor", "master", "phd", "doctor", "associate", "degree"))
            return "Yes" if has_degree else "No"
        if "experience" in label or "years" in label:
            return "Yes" if profile.get("years_experience") else "No"

    # High-priority deterministic guards — run BEFORE keyword FIELD_MAP so the
    # LLM never gets a chance to hallucinate Yes on credentials/experience.
    if any(k in label for k in (
        "certification", "license", "credential", "certified", "licensed",
        "registration", "registered"
    )):
        cred = _credential_answer(label, profile)
        if cred is not None:
            return cred
    if "years" in label and "experience" in label:
        yrs = _job_specific_years(label, profile)
        if yrs is not None:
            return yrs
    if "experience" in label and any(k in label for k in ("with ", "working ", " in ")):
        exp = _experience_with_answer(label, profile)
        if exp is not None:
            return exp
    if field.field_type in ("checkbox", "radio") and any(
        k in label for k in (
            "shift", "mornings", "evenings", "nights", "overnight",
            "weekends", "holidays", "graveyard"
        )
    ):
        shift = _shift_answer(label, profile)
        if shift is not None:
            return shift

    # "All" checkbox in a "select all shifts you can work" multi-select.
    # When the label is just "All" (no other context), it's almost certainly
    # the "I'm available for every shift type" option. With a M-F 8-5
    # availability we are NOT fully open — answer No so the form doesn't claim
    # 24/7 availability we don't have.
    if (field.field_type == "checkbox" and label.strip().lower() == "all"):
        avail = (profile.get("availability") or "").lower()
        fully_open = ("24/7" in avail or "any time" in avail or "open" in avail)
        return "Yes" if fully_open else "No"

    # When the form_analyzer picks up the boilerplate "Required fields are marked
    # with an asterisk (*)." as the label for a radio fieldset (Indeed's
    # demographic pages nest the real question in a sibling div), fall back to
    # inspecting the radio options themselves.
    if "required fields are marked" in label or "an asterisk" in label or not label.strip():
        opts_blob = " | ".join(field.options).lower()
        if "protected veteran" in opts_blob:
            return "I am not a protected veteran"
        if "have a disability" in opts_blob or "disability, or have had one" in opts_blob:
            return "No, I do not have a disability and have not had one in the past"
        if "hispanic or latino" in opts_blob and "asian" in opts_blob:
            return "White (Not Hispanic or Latino)"
        if "male" in opts_blob and "female" in opts_blob:
            return "Female"  # profile-aligned default

    # Applicant address city/state/zip (TEXT inputs). Indeed pre-fills these from the
    # user's saved Indeed profile, which can be stale (e.g. an old zip) — and the
    # caller trusts a pre-fill it can't map, so a wrong value rides along. Override
    # with our profile. Gated on text type (work-auth "...United States?" is a radio,
    # so it can't match here) and excludes employer/company location fields, which
    # FIELD_MAP maps to current_employer_* below.
    if field.field_type in ("text", "tel", "number") and not any(
            k in label for k in ("employer", "company", "united states")):
        if "zip" in label or "postal" in label:
            return profile.get("zip_code") or ""
        if "city" in label and "ethnicity" not in label:
            return profile.get("city") or ""
        if ("state" in label or "province" in label) and not field.options:
            return profile.get("state") or ""

    for pattern, profile_key in FIELD_MAP:
        if pattern in label:
            # Static-answer shortcuts.
            if profile_key == "_skip":
                return "__SKIP__"  # sentinel: caller must not fill or LLM-answer this field
            # Yes/No static answers only make sense for radio/checkbox/select.
            # Refusing them on typed inputs (text, tel, email, number) avoids
            # the regression where a phone field labeled "Enter number to
            # receive text messages" gets filled with the literal "Yes".
            if profile_key == "_yes":
                if field.field_type in ("text", "tel", "email", "number", "url"):
                    return None
                return "Yes"
            if profile_key == "_no":
                if field.field_type in ("text", "tel", "email", "number", "url"):
                    return None
                return "No"
            if profile_key == "_us":
                return "United States"
            if profile_key == "_resume_pdf":
                return "resume.pdf"
            if profile_key == "_today":
                from datetime import date
                return date.today().strftime("%m/%d/%Y")
            if profile_key == "_not_veteran":
                return profile.get("veteran_status") or "I am not a protected veteran"
            if profile_key == "_no_disability":
                # EEO self-ID radios phrase the negative as a full sentence; match
                # it so we never fall through and accidentally pick the "Yes" option.
                opts_blob = " | ".join(field.options).lower()
                if "have a disability" in opts_blob or "disability, or have had one" in opts_blob:
                    return "No, I do not have a disability and have not had one in the past"
                return profile.get("disability_status") or "No"
            if profile_key == "_agree":
                # For radios/checkboxes with a single agreement option, _best_option_match
                # will substring-pick whatever "agree" option exists.
                return "I have read and agree"
            if profile_key == "_end_date":
                # Employment-history End Date. If still in the role return
                # "Present"; otherwise leave blank for the LLM.
                if profile.get("currently_employed"):
                    return "Present"
                return None
            if profile_key == "_blank":
                # Deliberately empty (address line 2, current-role end year).
                # Returning "" also clears a stale Indeed pre-fill via the
                # override path, instead of leaving old data in the field.
                return ""
            if profile_key == "_start_year":
                # Year of the current/most-recent job's start (from
                # current_job_start_date). Keeps "Start year" fields off the
                # LLM, which otherwise guesses (this_year - years_experience).
                import re
                m = re.search(r"(19|20)\d{2}", str(profile.get("current_job_start_date") or ""))
                return m.group(0) if m else None
            # Special case: full name
            if profile_key == "_full_name":
                first = profile.get("first_name", "")
                last = profile.get("last_name", "")
                full = f"{first} {last}".strip()
                return full if full else None

            # "How did you hear about us?" — truthful: found via Indeed. The combobox
            # fill best-matches this against the real options (Indeed, Job board, …).
            if profile_key == "_how_heard":
                return "Indeed"

            if profile_key in ("desired_salary", "desired_hourly_rate"):
                is_hourly = (profile_key == "desired_hourly_rate") or any(
                    k in label for k in (
                        "hourly", "per hour", "/hr", "/ hour", "rate per hour",
                        "hour rate", "wage", "an hour",
                    )
                )
                return _salary_answer(job_data, profile, is_hourly=is_hourly)

            value = profile.get(profile_key)
            if value is None:
                return None

            # Convert booleans to Yes/No for form fields
            if isinstance(value, bool):
                return "Yes" if value else "No"

            # Convert ints to string
            if isinstance(value, int):
                return str(value)

            # Convert lists to comma-separated
            if isinstance(value, list):
                return ", ".join(value)

            return str(value)

    return None


def _best_option_match(target: str, options: list[str]) -> str | None:
    """Find the best matching option from a list of choices.

    Tries exact match first, then case-insensitive, then substring.
    """
    target_lower = target.lower().strip()

    # Exact match
    for opt in options:
        if opt.strip() == target.strip():
            return opt

    # Case-insensitive match
    for opt in options:
        if opt.lower().strip() == target_lower:
            return opt

    # Yes/No answers: match by the option's FIRST word, BEFORE any loose substring
    # pass. Otherwise "No" matches the "no" inside "now" in a "Yes, I will now..."
    # option and selects the wrong answer (this actually happened on sponsorship).
    if target_lower in ("yes", "y", "true", "no", "n", "false"):
        want_yes = target_lower in ("yes", "y", "true")
        for opt in options:
            words = opt.lower().strip().lstrip("([").split()
            first = words[0].rstrip(",.:;)") if words else ""
            if want_yes and first in ("yes", "y"):
                return opt
            if not want_yes and first in ("no", "n"):
                return opt
        for opt in options:
            o = opt.lower().strip()
            if want_yes and o.startswith("yes"):
                return opt
            if not want_yes and o.startswith("no"):
                return opt
        return None  # a Yes/No target must only match a Yes/No option, never a loose substring

    # Substring match (target contained in option)
    for opt in options:
        if target_lower in opt.lower():
            return opt

    # Substring match (option contained in target)
    for opt in options:
        if opt.lower().strip() in target_lower:
            return opt

    return None


async def dismiss_blocking_modal(page: Page) -> bool:
    """Dismiss Indeed interstitial modals (updated Terms of Service / arbitration,
    cookie consent) that overlay the apply form and intercept every click. Returns
    True if one was dismissed. Safe no-op when none is present."""
    labels = ["Accept Terms", "Accept all", "Accept cookies", "Accept",
              "Got it", "I agree", "I accept", "OK"]
    for lbl in labels:
        try:
            loc = page.get_by_role("button", name=lbl, exact=True).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=2500)
                print(f"  [form] Dismissed blocking modal: {lbl}")
                await human_delay(0.6, 1.2)
                return True
        except Exception:
            continue
    return False


async def click_apply_button(page: Page) -> bool:
    """Find and click the Easy Apply / Apply Now button on a job posting.

    Returns True if the apply form was opened successfully.
    """
    print("  [apply] Looking for Apply button...")

    # Try multiple selectors for the apply button. Indeed's newer viewjob layout
    # renders the apply control as an <a> anchor to smartapply (not a <button>),
    # so anchor/testid selectors come first; the old <button> forms remain as
    # fallbacks for the classic layout.
    selectors = [
        '#primary-apply-action',
        '[data-testid="viewjob-indeed-apply"]',
        'a[href*="smartapply.indeed.com"]',
        'button.indeed-apply-button',
        'button#indeedApplyButton',
        'button[data-testid="indeedApplyButton"]',
        'button:has-text("Apply now")',
        'button:has-text("Easily apply")',
        'a:has-text("Apply now")',
    ]

    for selector in selectors:
        try:
            btn = await page.query_selector(selector)
            if not (btn and await btn.is_visible()):
                continue
            print(f"  [apply] Found apply button: {selector}")
            await btn.scroll_into_view_if_needed(timeout=3000)

            # Smartapply is a plain <a>; navigating its href is more reliable than
            # clicking under Indeed's floating nav/portal, which intercepts clicks.
            href = await btn.get_attribute("href")
            if href and ("smartapply.indeed.com" in href or "indeedapply" in href):
                print(f"  [apply] Navigating to apply URL: {href[:80]}")
                await page.goto(href, wait_until="domcontentloaded")
                await human_delay(2, 4)
                return True

            # Otherwise click, escalating past overlays: normal -> force -> JS.
            try:
                await btn.click(timeout=4000)
            except Exception:
                try:
                    await btn.click(force=True, timeout=4000)
                except Exception:
                    await page.evaluate("el => el.click()", btn)
            await human_delay(2, 4)
            return True
        except Exception as e:
            print(f"  [apply] '{selector}' failed: {type(e).__name__}: {str(e)[:120]}")
            continue

    print("  [apply] No Apply button found on page")
    await dump_page(page, "apply-button-not-clickable", force=True)
    return False


def _yesno_option(value: str, texts: list[str]) -> str | None:
    """Map a Yes/No answer onto phrased dropdown options that have no literal
    'Yes'/'No' (e.g. 'Have you worked here?' -> 'I have not worked at DoorDash').
    Only the negative case is guessed confidently; a positive with only phrased
    options is left unmatched rather than risk picking the wrong affirmation.
    """
    v = value.strip().lower()
    if v not in ("no", "false", "n"):
        return None
    for t in texts:
        tl = t.lower()
        if ("have not" in tl or "haven't" in tl or "not worked" in tl
                or "none" in tl or "do not" in tl or "don't" in tl
                or tl.startswith("no ") or tl == "no"):
            return t
    return None


async def _fill_combobox(page: Page, field: FormField, value: str) -> bool:
    """Fill an Indeed RN-Web custom dropdown (<div role="combobox">): click to open,
    then click the option that best matches `value`. Options only exist once opened."""
    trigger = await page.query_selector(field.selector)
    if not trigger:
        print(f"    [fill] combobox trigger not found: {field.selector}")
        return False
    try:
        await trigger.scroll_into_view_if_needed(timeout=2000)
        await trigger.click()
        await human_delay(0.6, 1.1)
        # Options render in a popup listbox after opening. Use text_content (not
        # inner_text, which returns '' for options rendered off-screen) and fall
        # back to aria-label, so a long non-viewport option isn't silently dropped.
        opts = await page.query_selector_all('[role="option"]')
        pairs = []  # (element, text)
        for o in opts:
            t = (await o.text_content() or "").strip()
            if not t:
                t = (await o.get_attribute("aria-label") or "").strip()
            if t:
                pairs.append((o, t))
        if not pairs:
            print("    [fill] combobox opened but no options rendered")
            return False
        texts = [t for _, t in pairs]
        best = _best_option_match(value, texts)
        if not best:
            best = _yesno_option(value, texts)  # map Yes/No onto phrased options
        if not best:
            print(f"    [fill] no combobox option matched '{value}' in {texts[:8]}")
            return False
        async def committed() -> bool:
            # The selection "stuck" if the trigger now displays the chosen option.
            cur = ((await trigger.text_content()) or "").strip().lower()
            return best.lower() in cur or (cur and cur != before_txt)

        before_txt = ((await trigger.text_content()) or "").strip().lower()
        target = next((o for o, t in pairs if t == best), None)
        if target is not None:
            await target.scroll_into_view_if_needed(timeout=1500)
            await target.click()
            await human_delay(0.3, 0.6)
            if await committed():
                print(f"    [fill] combobox '{field.label_text[:40]}' -> {best!r}")
                return True
            # Some RN-Web comboboxes ignore a click on the option's inner node.
            # Retry by clicking the option's parent, then fall back to keyboard.
            try:
                await target.evaluate("el => (el.closest('[role=\\'option\\']') || el).click()")
                await human_delay(0.3, 0.6)
                if await committed():
                    print(f"    [fill] combobox '{field.label_text[:40]}' -> {best!r} (parent-click)")
                    return True
            except Exception:
                pass

        # Keyboard fallback: open, type-ahead the option text, confirm with Enter.
        await trigger.click()
        await human_delay(0.3, 0.6)
        await page.keyboard.type(best[:18], delay=25)
        await human_delay(0.3, 0.6)
        await page.keyboard.press("Enter")
        await human_delay(0.3, 0.6)
        if await committed():
            print(f"    [fill] combobox '{field.label_text[:40]}' -> {best!r} (keyboard)")
            return True
        print(f"    [fill] combobox did NOT commit '{best}' (trigger still {before_txt!r})")
        return False
    except Exception as e:
        print(f"    [fill] combobox fill failed: {type(e).__name__}: {str(e)[:80]}")
        return False


async def fill_field(page: Page, field: FormField, value: str):
    """Fill a single form field with the given value using Playwright."""
    if not field.selector:
        print(f"    [fill] No selector for field: {field.label_text[:50]}")
        return

    try:
        if field.field_type in ("text", "tel", "email", "number", "url"):
            el = await page.query_selector(field.selector)
            if el:
                # Inspect the actual DOM — some text inputs declare
                # `inputmode="numeric"` and reject any non-digit content
                # (e.g. Indeed's "Phone Number" with +/-/spaces in value).
                inputmode = await el.get_attribute("inputmode") or ""
                el_type = await el.get_attribute("type") or ""
                pattern = await el.get_attribute("pattern") or ""
                if (field.field_type == "number"
                        or inputmode.lower() == "numeric"
                        or el_type.lower() == "number"
                        or "\\d" in pattern):
                    digits_only = "".join(ch for ch in str(value) if ch.isdigit())
                    if digits_only:
                        value = digits_only
                await el.click()
                await human_delay(0.2, 0.4)
                await el.fill("")
                await el.fill(value)
                await human_delay(0.3, 0.6)

        elif field.field_type == "textarea":
            el = await page.query_selector(field.selector)
            if el:
                await el.click()
                await human_delay(0.2, 0.4)
                await el.fill("")
                await el.fill(value)
                await human_delay(0.3, 0.6)

        elif field.field_type == "select":
            best = _best_option_match(value, field.options)
            if best:
                await page.select_option(field.selector, label=best)
                await human_delay(0.3, 0.5)
            else:
                print(f"    [fill] No matching option for '{value}' in {field.options[:5]}")

        elif field.field_type == "combobox":
            await _fill_combobox(page, field, value)

        elif field.field_type == "radio":
            # Primary match against the options form_analyzer extracted.
            best = _best_option_match(value, field.options)
            # If form_analyzer pulled raw values (e.g. "VETERAN_STATUS-6-5")
            # instead of human labels, _best_option_match can't help. In that
            # case we fall through to live label extraction below.
            if not best:
                best = value  # try matching against radio's actual label text
            if best:
                # Find the specific radio button whose label matches
                radios = await page.query_selector_all(field.selector)
                for radio in radios:
                    radio_label = await radio.evaluate("""el => {
                        // Prefer the standard DOM API — it returns the label(s)
                        // explicitly associated with this input, not an ancestor
                        // that happens to wrap multiple radios.
                        if (el.labels && el.labels.length > 0) {
                            return Array.from(el.labels)
                                .map(l => l.textContent.trim())
                                .join(' ').trim();
                        }
                        // Fall back to closest label, but ONLY if it doesn't
                        // wrap other radios in the same group.
                        const label = el.closest('label');
                        if (label) {
                            const otherRadios = label.querySelectorAll(
                                'input[type="radio"][name="' + el.name + '"]'
                            );
                            if (otherRadios.length <= 1) {
                                return label.textContent.trim();
                            }
                        }
                        // Aria + sibling fallbacks.
                        const aria = el.getAttribute('aria-label');
                        if (aria) return aria.trim();
                        const next = el.nextElementSibling;
                        if (next) return next.textContent.trim();
                        return el.value;
                    }""")
                    # Use exact / boundary-aware match instead of bidirectional substring —
                    # "Male" should NOT match "Male Female Decline".
                    radio_label_norm = (radio_label or "").strip().lower()
                    best_norm = best.strip().lower()
                    if not radio_label_norm:
                        continue
                    is_match = (
                        radio_label_norm == best_norm
                        or radio_label_norm.startswith(best_norm + " ")
                        or radio_label_norm.endswith(" " + best_norm)
                        or f" {best_norm} " in f" {radio_label_norm} "
                    )
                    if is_match:
                        try:
                            await radio.click(timeout=2000)
                        except Exception:
                            await page.evaluate(
                                "(e) => { e.checked = true; "
                                "e.dispatchEvent(new Event('change', {bubbles:true})); "
                                "e.dispatchEvent(new Event('click', {bubbles:true})); }",
                                radio,
                            )
                        await human_delay(0.2, 0.4)
                        break
            else:
                print(f"    [fill] No matching radio option for '{value}'")

        elif field.field_type == "checkbox":
            if value.lower() in ("yes", "true", "1"):
                el = await page.query_selector(field.selector)
                if el:
                    is_checked = await el.is_checked()
                    if not is_checked:
                        # Indeed hides the real <input> behind a custom label;
                        # a normal click can hang on actionability for 30s.
                        try:
                            await el.click(timeout=2000)
                        except Exception:
                            await page.evaluate(
                                "(e) => { e.checked = true; "
                                "e.dispatchEvent(new Event('change', {bubbles:true})); "
                                "e.dispatchEvent(new Event('input', {bubbles:true})); }",
                                el,
                            )
                        await human_delay(0.2, 0.3)

        safe_label = field.label_text[:40].encode("ascii", errors="replace").decode("ascii")
        safe_value = value[:40].encode("ascii", errors="replace").decode("ascii")
        print(f"    [fill] {safe_label} = {safe_value}")

        # Audit log: append every (question, answer) the agent gave, skipping
        # file uploads (not a Q→A) and ungrounded blanks.
        if field.field_type != "file" and value not in (None, ""):
            qa_logger.log(field.label_text, value)

    except Exception as e:
        print(f"    [fill] Error filling '{field.label_text[:40]}': {e}")


async def select_upload_resume_option(page: Page) -> bool:
    """Select the 'Upload a resume' radio safely (no synthetic click that
    would open the native OS file dialog). Delegates to the JS-event path."""
    return await _pick_upload_resume_radio(page)


async def upload_resume(page: Page, field: FormField, resume_path: str):
    """Upload a resume PDF file via a file input field.

    Always selects the 'Upload a resume' option first when a picker is present,
    so we never use Indeed's stored / AI-tailored resume.
    """
    try:
        # If Indeed shows a resume-source picker, choose 'Upload' before the file input.
        await select_upload_resume_option(page)

        if field.selector:
            el = await page.query_selector(field.selector)
            if el:
                await el.set_input_files(resume_path)
                print(f"    [fill] Uploaded resume: {resume_path}")
                await human_delay(1, 2)
                return

        # Fallback: find any file input (often hidden but functional)
        file_inputs = await page.query_selector_all('input[type="file"]')
        for fi in file_inputs:
            await fi.set_input_files(resume_path)
            print(f"    [fill] Uploaded resume via fallback: {resume_path}")
            await human_delay(1, 2)
            return

        print(f"    [fill] WARNING: Could not find file input to upload resume")
    except Exception as e:
        print(f"    [fill] Error uploading resume: {e}")


async def click_continue(page: Page) -> bool:
    """Click the Continue/Next button to advance to the next form step.

    Returns True if a continue button was found and clicked.
    """
    selectors = [
        'button[data-testid="continue-button"]',
        'button[data-testid^="hp-continue-button"]',
        'button#form-action-continue',
        'button[data-testid="form-action-continue"]',
        'button[data-testid="IndeedApplyButton-continue"]',
        'button:has-text("Continue applying")',
        'button:has-text("Save and Continue")',
        'button:has-text("Review your application")',
        'button:has-text("Review application")',
        'button:has-text("Apply anyway")',
        'button:has-text("Continue")',
        'button:has-text("Next")',
        'button[aria-label="Continue"]',
        'button.ia-continueButton',
        'div[role="button"]:has-text("Continue")',
    ]

    # Indeed often re-renders the Continue button a beat after upload completes.
    for attempt in range(3):
        for selector in selectors:
            try:
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    try:
                        await btn.click()
                    except Exception:
                        await page.evaluate("(e) => e.click()", btn)
                    await human_delay(1.5, 3.0)
                    return True
            except Exception:
                continue
        if attempt < 2:
            await human_delay(1.5, 2.5)

    print("  [form] No Continue/Next button found")
    return False


async def click_submit(page: Page) -> bool:
    """Click the final Submit button.

    In DRY_RUN mode this dumps the would-be-submit page and reports success
    WITHOUT actually clicking, so we can iterate on form coverage without
    submitting real applications.

    Returns True if the submit button was found (and clicked, unless DRY_RUN).
    """
    selectors = [
        'button#form-action-submit',
        'button[data-testid="form-action-submit"]',
        'button[data-testid="submit-application-button"]',
        'button[data-testid*="submit" i]',
        'button:has-text("Submit your application")',
        'button:has-text("Submit application")',
        'button:has-text("Submit")',
        'button[type="submit"]',
        # Smartapply renders controls as React-Native-Web <div role="button">,
        # not native <button> — match those too.
        '[role="button"]:has-text("Submit your application")',
        '[role="button"]:has-text("Submit application")',
        '[data-testid*="submit" i]',
    ]

    # Review pages render the Submit button a beat after navigation. Wait for
    # network idle to give React a chance to finish rendering before polling.
    try:
        await page.wait_for_load_state("networkidle", timeout=6000)
    except Exception:
        pass
    await human_delay(1.5, 2.5)

    # Retry with progressive backoff. Doubled vs prior version because some
    # employer-customized review pages take 15-20s to mount the Submit button.
    for attempt in range(8):
        for selector in selectors:
            try:
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    if DRY_RUN:
                        print(f"  [form] DRY-RUN: would click Submit ({selector}) — skipping")
                        await dump_page(page, "would-submit", force=True)
                        field_inventory.note(f"would-submit at {page.url}")
                        return True
                    print("  [form] Clicking Submit button...")
                    await btn.click()
                    await human_delay(2, 4)
                    return True
            except Exception:
                continue
        if attempt < 7:
            await human_delay(2, 3)

    print("  [form] No Submit button found")
    await dump_page(page, "submit-button-missing", force=True)
    return False


async def _is_resume_source_picker(page: Page) -> bool:
    """Return True if the current step is Indeed's 'Use Indeed Resume vs Upload' chooser."""
    try:
        return await page.evaluate("""() => {
            const t = (document.body.innerText || '').toLowerCase();
            const hasUseIndeed = t.includes('use your indeed resume') || t.includes('indeed resume');
            const hasUpload = t.includes('upload a resume') || t.includes('upload your resume') || t.includes('upload resume');
            return hasUseIndeed && hasUpload;
        }""")
    except Exception:
        return False


async def _pick_upload_resume_radio(page: Page) -> bool:
    """Select the 'Upload a resume' option on the resume-source picker.

    IMPORTANT: avoid clicking Indeed's upload-card button — it internally
    triggers `.click()` on the hidden file input, which opens a native OS
    file-picker dialog that never closes from Playwright's side. Instead we
    drive the underlying radio input directly via JS (no synthetic click).
    """
    # Drive the radio selection without dispatching a synthetic click.
    try:
        picked = await page.evaluate("""() => {
            const r = document.querySelector(
              'input[type="radio"][value="file"], ' +
              'input[type="radio"][value*="upload" i], ' +
              'input[type="radio"][id*="upload" i]'
            );
            if (!r) return false;
            r.checked = true;
            r.dispatchEvent(new Event('input', { bubbles: true }));
            r.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        }""")
        if picked:
            print("    [resume] Selected Upload radio via JS event")
            return True
    except Exception:
        pass
    return False


async def _upload_our_resume(page: Page, resume_path: str) -> bool:
    """Swap in our tuned PDF over Indeed's stored/AI resume. Returns True on upload.
    Shared by the dedicated resume-source step AND a review page that embeds the
    resume-selection module (a minimal apply can land straight on review)."""
    await _pick_upload_resume_radio(page)   # harmless if no text-picker
    await human_delay(0.5, 1.0)
    # Prefer the resume-specific file input; fall back to any file input.
    fi = (await page.query_selector('input[type=file][data-testid*="resume" i]')
          or await page.query_selector('input[type="file"]'))
    if not fi:
        return False
    try:
        await fi.set_input_files(resume_path)
        await human_delay(3, 5)   # let Indeed parse the upload
        return True
    except Exception as e:
        print(f"    [fill] Resume upload failed: {e}")
        return False


async def _review_includes_resume(page: Page) -> bool:
    """True if the review/summary page actually INCLUDES a resume in the application
    (a 'Resume' section heading, an attached .pdf/.doc filename, or a resume element).
    Scoped to the apply form's main content so global nav/footer 'Resume' links don't
    false-positive. Used to decide whether reaching review WITHOUT our tuned resume
    means the wrong resume would be sent (skip) or no resume is required (proceed)."""
    try:
        return await page.evaluate(r"""() => {
            const main = document.querySelector('main, [role=main], #ia-container, .ia-BasePage-content') || document.body;
            const t = main.innerText || '';
            const heads = [...main.querySelectorAll('h1,h2,h3,h4,[role=heading]')].map(h => (h.innerText||'').trim());
            const hasResumeHeading = heads.some(h => /\bresume\b/i.test(h));
            const hasFile = /[A-Za-z0-9 _().\-]+\.(pdf|docx?)/i.test(t);
            const hasResumeEl = !!main.querySelector('[data-testid*="resume" i]');
            return hasResumeHeading || hasFile || hasResumeEl;
        }""")
    except Exception:
        return False  # unknown -> treat as no-resume (don't block applying)


_MODAL_DUMPED = False  # dump the edit-modal DOM only once per process (probe aid)


async def _fix_resume_review_cards(page: Page, profile: dict) -> bool:
    """On Indeed's resume-parse review, each work-experience card can be flagged
    'required details missing'. The parser commonly drops the STATE (location shows
    only a city) and/or the END DATE (date-range shows only a start). Open each
    flagged card's edit modal, fill the missing fields from the profile work_history,
    and Save. Returns True if any card was fixed. Bounded loop; re-queries each pass
    since the DOM re-renders after every Save.
    """
    global _MODAL_DUMPED
    locs = profile.get("employer_locations") or {}
    home = f"{profile.get('city', '')}, {profile.get('state', '')}".strip(", ")
    history = profile.get("work_history") or []

    def match_hist(company: str) -> dict:
        cl = (company or "").lower()
        for h in history:
            emp = (h.get("employer") or "").lower()
            if emp and (emp in cl or cl in emp):
                return h
        return {}

    def lookup_loc(company: str, hist: dict) -> str:
        if hist.get("location"):
            return hist["location"]
        cl = (company or "").lower()
        for k, v in locs.items():
            if k.lower() in cl or (cl and cl in k.lower()):
                return v
        return home  # fall back to the candidate's own city/state

    # Wait for the parsed work-experience cards to render.
    try:
        await page.wait_for_selector('[data-testid="workExperience-card"]', timeout=8000)
    except Exception:
        return False
    print("  [resume-review] checking work-experience cards for missing details...")

    fixed = 0
    done = set()  # card indices already edited — the validation-error span persists
    for _ in range(8):  # at most 8 cards
        # Flag a card when it carries a validation-error, OR when the parser left an
        # obviously-incomplete location (no state, i.e. no comma) or date range (no
        # end, i.e. no "-"/"present"). The error span is the authoritative signal
        # once Continue was attempted; the heuristics catch it beforehand.
        info = await page.evaluate("""() => {
            const cards = [...document.querySelectorAll('[data-testid="workExperience-card"]')];
            for (let i = 0; i < cards.length; i++) {
                const c = cards[i];
                const err = c.querySelector('[data-testid="workExperience-card-validation-error"]');
                const locEl = c.querySelector('[data-testid="workExperience-card-location"]');
                const loc = locEl ? locEl.textContent.trim() : '';
                const drEl = c.querySelector('[data-testid="workExperience-card-date-range"]');
                const dr = drEl ? drEl.textContent.trim().toLowerCase() : '';
                const badLoc = !loc || loc.toLowerCase() === 'city, state' || loc.indexOf(',') === -1;
                const badDate = dr && dr.indexOf('-') === -1 && dr.indexOf('–') === -1 && dr.indexOf('present') === -1;
                if (err || badLoc || badDate) {
                    const p = c.querySelector('[data-testid="workExperience-card-place"]');
                    return { index: i, company: p ? p.textContent.trim() : '', loc, dr };
                }
            }
            return null;
        }""")
        if not info:
            break
        if info["index"] in done:
            break  # only remaining flags are cards we already edited (stale error span)
        done.add(info["index"])
        hist = match_hist(info["company"])
        loc = lookup_loc(info["company"], hist)
        btns = await page.query_selector_all('[data-testid="workExperience-card-edit-btn"]')
        if info["index"] >= len(btns):
            break
        try:
            await btns[info["index"]].click()
            await human_delay(1, 1.5)

            # One-time probe: print every control in the edit modal so we learn the
            # date / title testids we don't yet know. Shows in the run log.
            if not _MODAL_DUMPED:
                _MODAL_DUMPED = True
                controls = await page.evaluate("""() => {
                    const out = [];
                    document.querySelectorAll('input, select, textarea, [role="combobox"], [role="checkbox"], [role="switch"]').forEach(el => {
                        if (!el.offsetParent) return;
                        out.push({
                            tag: el.tagName.toLowerCase(),
                            type: el.getAttribute('type') || el.getAttribute('role') || '',
                            testid: el.getAttribute('data-testid') || '',
                            label: (el.getAttribute('aria-label') || el.placeholder || '').slice(0,60),
                            value: (el.value || '').slice(0,40),
                            checked: el.getAttribute('aria-checked') || el.checked || ''
                        });
                    });
                    return out;
                }""")
                print("    [resume-review] EDIT MODAL CONTROLS:")
                for c in controls:
                    print(f"      {c}")

            # Fill Job Title if the parser left it blank (this is the usual missing
            # required field — the empty title is what flags the card).
            title_inp = await page.query_selector('[data-testid="work-experience-page-title-input"]')
            if title_inp and not (await title_inp.input_value()).strip() and hist.get("job_title"):
                await title_inp.click()
                await title_inp.fill(hist["job_title"])
                await human_delay(0.4, 0.8)

            # Fill City/State. Overwrite whenever the current value lacks a comma
            # (state missing) — not only when empty.
            inp = await page.query_selector('[data-testid="work-experience-page-locality-input"]')
            if inp:
                cur = (await inp.input_value()).strip()
                if "," not in cur and loc:
                    await inp.click()
                    await inp.fill(loc)
                    await human_delay(0.8, 1.2)
                    await inp.press("Tab")  # commit value, close the suggestion dropdown

            # End date: if history says this role has ended, uncheck "I currently
            # work here" (the lone unlabeled checkbox, which hides the to-* selects)
            # and set the to-month/to-year. end_date is "MM/YYYY"; "Present" leaves
            # the checkbox checked and no end date, which is valid.
            end = str(hist.get("end_date", "")).strip()
            if end and end.lower() != "present" and "/" in end:
                mm, yy = end.split("/")[0].lstrip("0") or "0", end.split("/")[1]
                cb = await page.query_selector('input[type="checkbox"]')
                if cb and await cb.is_checked():
                    await cb.click()
                    await human_delay(0.5, 0.9)
                for sel, val in (("work-experience-page-to-month", mm),
                                 ("work-experience-page-to-year", yy)):
                    el = await page.query_selector(f'[data-testid="{sel}"]')
                    if el:
                        try:
                            await el.select_option(value=val)
                        except Exception:
                            pass

            save = await page.query_selector('[data-testid="work-experience-page-edit-save-button"]')
            if save:
                await save.click()
                await human_delay(1.5, 2.5)
                fixed += 1
                print(f"    [resume-review] {info['company']}: loc->{loc} (was loc='{info['loc']}' dr='{info['dr']}')")
            else:
                cancel = await page.query_selector('[data-testid="work-experience-page-edit-cancel-button"]')
                if cancel:
                    await cancel.click()
                break
        except Exception as e:
            print(f"    [resume-review] fix '{info['company']}' failed: {e}")
            break
    return fixed > 0


def _demographic_answer(label: str, profile: dict) -> str | None:
    """Target option text for a demographic custom-combobox, from its label."""
    l = (label or "").lower()
    if "country" in l:
        return "United States"
    if "race" in l:
        eth = (profile.get("ethnicity") or "").lower()
        if "black" in eth: return "Black or African American"
        if "asian" in eth: return "Asian"
        if "two or more" in eth: return "Two or More Races"
        if "hispanic" in eth and "not hispanic" not in eth: return "Hispanic or Latino"
        if "american indian" in eth or "alaska" in eth: return "American Indian or Alaska Native"
        if "hawaiian" in eth or "pacific" in eth: return "Native Hawaiian or Other Pacific Islander"
        return "White"  # profile default
    return None


async def _fill_demographic_comboboxes(page: Page, profile: dict) -> bool:
    """Fill Indeed's custom demographic dropdowns (Country / Race). These are
    role="combobox" divs (not <select>), so analyze_form_page misses them; if left
    empty the 'Choose an option' validation blocks Continue. Click each unfilled
    box, filter to the target option, and click it."""
    filled = 0
    for _ in range(6):
        info = await page.evaluate("""() => {
            const boxes = [...document.querySelectorAll(
                '[data-testid="single-select-question-select-list-select-list"][role="combobox"]')];
            for (let i = 0; i < boxes.length; i++) {
                const t = (boxes[i].innerText || '').trim().toLowerCase();
                if (t.includes('select an option')) {
                    let lab = ''; const lb = boxes[i].getAttribute('aria-labelledby');
                    if (lb) { const el = document.getElementById(lb.split(' ')[0]); if (el) lab = el.innerText; }
                    return { index: i, label: lab };
                }
            }
            return null;
        }""")
        if not info:
            break
        target = _demographic_answer(info["label"], profile)
        if not target:
            break
        boxes = await page.query_selector_all(
            '[data-testid="single-select-question-select-list-select-list"][role="combobox"]')
        if info["index"] >= len(boxes):
            break
        try:
            await boxes[info["index"]].click()
            await human_delay(0.6, 1.0)
            # Click the option INSIDE the currently-open dropdown only (scope by the
            # expanded combobox's aria-controls popup) so we never touch another
            # dropdown's stale/hidden filter input.
            picked = await page.evaluate("""(target) => {
                const box = [...document.querySelectorAll(
                    '[data-testid="single-select-question-select-list-select-list"][role="combobox"]')]
                    .find(b => b.getAttribute('aria-expanded') === 'true');
                const pop = box ? document.getElementById(box.getAttribute('aria-controls')) : null;
                const scope = pop || document;
                const opts = [...scope.querySelectorAll('[role="option"]')];
                const o = opts.find(x => x.innerText.trim().toLowerCase() === target.toLowerCase());
                if (!o) return false;
                o.scrollIntoView(); o.click();
                return true;
            }""", target)
            await human_delay(0.6, 1.0)
            if picked:
                filled += 1
                print(f"    [demographic] {info['label'][:30]!r} -> {target}")
            else:
                print(f"    [demographic] no option '{target}' in open dropdown")
                break
        except Exception as e:
            print(f"    [demographic] combobox fill failed: {e}")
            break
    return filled > 0


async def fill_and_submit_application(page: Page, user_profile: dict,
                                       job_data: dict, resume_path: str,
                                       model: str = "claude-haiku-4-5",
                                       max_steps: int = 15) -> tuple[str, dict]:
    """Complete the entire Indeed Easy Apply flow.

    Handles multi-step forms by analyzing each page, filling fields,
    and clicking through until submission.

    Args:
        page: Playwright page with the job posting loaded.
        user_profile: Full user profile dict.
        job_data: Current job posting data for LLM context.
        resume_path: Path to resume PDF to upload.
        model: Claude model ID for LLM answering.
        max_steps: Safety limit on form pages.

    Returns:
        Tuple of (status, answers_dict) where status is one of:
        'applied', 'already_applied', 'failed', 'skipped'
    """
    all_answers = {}

    # Safety net: if any stray code path triggers a native file chooser,
    # auto-fill it with the resume so the dialog closes immediately and
    # never blocks the loop.
    async def _auto_filechooser(fc):
        try:
            await fc.set_files(resume_path)
            print("  [form] Auto-handled stray file chooser dialog")
        except Exception as e:
            print(f"  [form] Auto-filechooser error: {e}")
    page.on("filechooser", lambda fc: asyncio.create_task(_auto_filechooser(fc)))

    # Check if already applied
    if await detect_already_applied(page):
        print("  [form] Already applied to this job, skipping")
        return "already_applied", all_answers

    # Click the Apply button
    if not await click_apply_button(page):
        return "failed", all_answers

    # Wait for the form to load
    await human_delay(2, 3)

    resume_done = False  # upload our tuned PDF exactly once
    stuck_url = None
    stuck_n = 0
    for step in range(max_steps):
        print(f"\n  [form] === Step {step + 1}/{max_steps} ===")

        # Indeed drops interstitial modals (updated ToS/arbitration, cookies) over
        # the form; they intercept every click until dismissed. Clear first.
        await dismiss_blocking_modal(page)

        # Check if we've reached the review/submit page
        if await is_review_page(page):
            # A minimal Indeed Apply can land directly on review with the
            # resume-selection module embedded (no dedicated resume step upstream),
            # so upload our tuned PDF HERE too — never submit Indeed's stored resume.
            if not resume_done and resume_path:
                has_resume_module = await page.evaluate(
                    "() => !!document.querySelector("
                    "'input[name=\"resume-selection\"], "
                    "input[type=file][data-testid*=\"resume\" i]')")
                if has_resume_module and await _upload_our_resume(page, resume_path):
                    all_answers["resume"] = resume_path
                    resume_done = True
                    print(f"    [fill] Uploaded resume on review page: {resume_path}")
            if resume_path and not resume_done:
                # We reached review without swapping in our tuned PDF (Indeed smartapply's
                # fast path auto-attaches the profile's SAVED resume and offers no in-flow
                # swap). Policy (per user): if this application actually INCLUDES a resume,
                # submitting would ship the WRONG (untuned) resume — SKIP the job entirely,
                # even in live runs. If the application requires NO resume, applying is fine.
                if await _review_includes_resume(page):
                    all_answers["resume_not_used"] = True
                    print("  [resume] SKIP: review includes a resume we could NOT replace "
                          f"with our tuned {resume_path} (smartapply has no swap control). "
                          "Job requires a resume + wrong one would be sent -> not applying.")
                    return "skipped_resume_required", all_answers
                print("  [resume] No resume in this application (none required) -> ok to submit.")
            print("  [form] Review page detected, submitting...")
            if await click_submit(page):
                await human_delay(2, 4)
                if await is_confirmation_page(page):
                    print("  [form] Application confirmed!")
                    return "applied", all_answers
                # Even if we don't see confirmation, the submit click likely worked
                return "applied", all_answers
            else:
                return "failed", all_answers

        # Check for confirmation page (might skip review on some applications)
        if await is_confirmation_page(page):
            print("  [form] Application confirmed!")
            return "applied", all_answers

        # Dump every form page in dry-run for offline review
        if DRY_RUN:
            await dump_page(page, f"form-step-{step + 1}", force=True)

        # Resume step. Indeed shows one of two shapes:
        #   (a) a "Use your Indeed Resume" vs "Upload a resume" text picker, or
        #   (b) a card with a PREVIOUSLY-UPLOADED resume already selected
        #       (name="resume-selection", value="file" checked) + an "Upload a
        #       different file" option and a hidden resume file input.
        # In BOTH cases upload OUR tuned PDF — never keep Indeed's stored/AI resume.
        if not resume_done and resume_path:
            cur = page.url.lower()
            is_resume_step = ("resume-selection" in cur) or await page.evaluate("""() => {
                const t = (document.body.innerText || '').toLowerCase();
                if (t.includes('upload a resume') || t.includes('use your indeed resume')) return true;
                if (t.includes('upload or create a resume') || t.includes('upload a different')) return true;
                if (document.querySelector('input[name="resume-selection"]')) return true;
                if (document.querySelector('input[type=file][data-testid*="resume" i]')) return true;
                return false;
            }""")
            if is_resume_step:
                print("  [form] Resume step detected — uploading our tuned resume")
                uploaded = await _upload_our_resume(page, resume_path)
                # Newer RN-Web resume-selection shows a preview card + a
                # "Use a different resume" button that opens a file chooser (handled
                # by the filechooser auto-handler above). Click it when there's no
                # direct file input to swap our tuned PDF in over Indeed's stored one.
                if not uploaded:
                    for name in ("Use a different resume", "Upload a different file",
                                 "Upload a resume", "Replace"):
                        try:
                            loc = page.get_by_role("button", name=name, exact=False).first
                            if await loc.count() and await loc.is_visible():
                                print(f"  [form] Clicking '{name}' to upload our resume")
                                await loc.click(timeout=3000)
                                await human_delay(2, 4)  # let file chooser auto-fill + parse
                                uploaded = True
                                break
                        except Exception:
                            continue
                if uploaded:
                    all_answers["resume"] = resume_path
                    resume_done = True
                    print(f"    [fill] Uploaded resume: {resume_path}")
                await click_continue(page)
                continue

        # Analyze the current form page
        fields = await analyze_form_page(page)
        field_inventory.record_page(fields, job_url=page.url, step=step + 1)

        if not fields:
            cur_url = page.url.lower()
            print("  [form] No form fields found on this page")
            await dump_page(page, f"form-step-{step + 1}-no-fields", force=True)

            # Bail if we've been stuck on the same URL — avoids burning all the
            # steps clicking a Continue that doesn't advance the SPA.
            stuck_n = stuck_n + 1 if cur_url == stuck_url else 0
            stuck_url = cur_url
            if stuck_n >= 3:
                print(f"  [form] Stuck on {cur_url} for {stuck_n + 1} steps — giving up")
                return "failed", all_answers

            # Indeed's resume-parse review (resume-module/structured-data-*) has no
            # standard fields; a plain click_continue clicks the button but the SPA
            # doesn't move. Force-click the footer continue-button after letting the
            # parse settle and scrolling it into view.
            if "resume-module" in cur_url or "structured-data" in cur_url:
                try:
                    # Fill missing City/State on parsed work-experience cards first,
                    # otherwise the review page's Continue button stays inert.
                    if "structured-data-review" in cur_url:
                        if await _fix_resume_review_cards(page, user_profile):
                            stuck_n = 0  # made progress — reset the stuck counter
                    await human_delay(2, 3)
                    loc = page.get_by_test_id("continue-button").last
                    await loc.scroll_into_view_if_needed(timeout=3000)
                    await loc.click(force=True, timeout=4000)
                    await human_delay(2.5, 4)
                    if page.url.lower() != cur_url:
                        continue
                except Exception as e:
                    print(f"  [form] structured-data continue failed: {e}")

            if await click_continue(page):
                continue
            # Submit fallback only on the actual review URL — avoids triggering
            # a submit-button-missing dump on demographic / interstitial pages.
            if "/review" in page.url.lower() or await is_review_page(page):
                print("  [form] No Continue — trying Submit (review page)")
                if await click_submit(page):
                    await human_delay(2, 4)
                    if await is_confirmation_page(page):
                        print("  [form] Application confirmed!")
                    return "applied", all_answers
            return "failed", all_answers

        # Process each field
        for field in fields:
            # Skip file inputs (handle resume separately)
            if field.field_type == "file":
                await upload_resume(page, field, resume_path)
                all_answers["resume"] = resume_path
                continue

            # Pre-filled fields (Indeed remembers prior answers via "Save my
            # answers"). Trust these UNLESS our deterministic FIELD_MAP /
            # guard logic disagrees — past hallucinations get re-saved by
            # Indeed and re-served on future runs, and we want to overwrite.
            # If the pre-filled value already exceeds the field's max length,
            # treat it as needing a fresh LLM answer with the cap respected.
            if (field.current_value and field.max_length
                    and len(field.current_value) > field.max_length):
                print(f"    [override] Pre-filled value {len(field.current_value)}c exceeds "
                      f"limit {field.max_length}c — re-generating")
                field.current_value = ""  # let normal fill path handle it

            if field.current_value:
                guard_value = map_field_to_profile(field, user_profile, job_data)
                # Refuse override when the guard returned a boolean (Yes/No)
                # but the existing pre-fill is clearly a typed value (phone,
                # email, address, zip) — the FIELD_MAP keyword match was
                # incidental and would destroy good data. e.g. a phone field
                # labeled "Enter number to receive text messages" matches the
                # SMS-consent pattern but actually needs the phone number.
                _gv = str(guard_value or "").strip().lower()
                _cv = field.current_value.strip()
                is_boolean_guard = _gv in ("yes", "no", "true", "false")
                is_typed_field = bool(_cv) and (
                    any(ch.isdigit() for ch in _cv) or "@" in _cv
                    or len(_cv) > 20
                )
                if is_boolean_guard and is_typed_field:
                    safe_label = field.label_text[:40]
                    print(f"    [skip] Pre-filled (refused boolean override): {safe_label} = {_cv[:30]}")
                    field_inventory.record_handler(field, "prefilled")
                    continue
                if (
                    guard_value is not None
                    and guard_value != "__SKIP__"
                    and str(guard_value).strip().lower() != field.current_value.strip().lower()
                ):
                    safe_label = field.label_text[:40]
                    safe_old = field.current_value[:30]
                    safe_new = str(guard_value)[:30]
                    print(f"    [override] Pre-filled {safe_label}: {safe_old!r} -> {safe_new!r}")
                    await fill_field(page, field, guard_value)
                    all_answers[field.label_text or field.name] = guard_value
                    field_inventory.record_handler(field, "keyword", value=guard_value)
                    continue
                safe_label = field.label_text[:40]
                safe_val = field.current_value[:30]
                print(f"    [skip] Pre-filled: {safe_label} = {safe_val}")
                field_inventory.record_handler(field, "prefilled")
                continue

            # Try direct keyword mapping first
            value = map_field_to_profile(field, user_profile, job_data)
            handler = "keyword" if value is not None else None

            # Explicit skip sentinel — don't fill, don't LLM, don't even log.
            if value == "__SKIP__":
                field_inventory.record_handler(field, "skipped")
                continue

            # Fall back to LLM answering for unknown fields
            if value is None and field.label_text:
                value = answer_question(
                    question=field.label_text,
                    field_type=field.field_type,
                    options=field.options,
                    user_profile=user_profile,
                    job_data=job_data,
                    model=model,
                    max_length=field.max_length,
                )
                # If the LLM still overshoots the field's maxLength, re-ask
                # with a stricter target. The model occasionally ignores the
                # first instruction; a second pass with a smaller cap usually
                # works.
                if (value and field.max_length
                        and len(str(value)) > field.max_length):
                    print(f"    [llm] Answer {len(value)}c exceeds limit "
                          f"{field.max_length}c — re-asking with tighter cap")
                    tighter_cap = max(50, int(field.max_length * 0.6))
                    value = answer_question(
                        question=field.label_text + f" (Be brief — under {tighter_cap} characters total)",
                        field_type=field.field_type,
                        options=field.options,
                        user_profile=user_profile,
                        job_data=job_data,
                        model=model,
                        max_length=tighter_cap,
                    )
                if value is not None:
                    handler = "llm"

            if value is not None:
                await fill_field(page, field, value)
                all_answers[field.label_text or field.name] = value
                field_inventory.record_handler(field, handler, value=value)
            elif field.required:
                print(f"    [warn] Required field unanswered: {field.label_text[:60]}")
                field_inventory.record_handler(field, "unhandled")
                await dump_page(page, "unhandled-required-field", force=True)

        # Fill Indeed's custom demographic dropdowns (Country / Race) — role="combobox"
        # divs that analyze_form_page can't see; unfilled ones block Continue.
        await _fill_demographic_comboboxes(page, user_profile)

        # Some fields are revealed dynamically by an earlier choice (e.g. a
        # country select unlocks a state select on the same page). Re-scan
        # the page and fill any newly-surfaced fields before clicking Continue.
        # Bounded loop so a buggy form can't keep adding fields forever.
        already_seen = {(f.label_text, f.selector) for f in fields}
        for _rescan in range(3):
            await human_delay(0.6, 1.2)
            fresh = await analyze_form_page(page)
            new_fields = [
                f for f in fresh
                if (f.label_text, f.selector) not in already_seen
                and f.field_type != "file"
            ]
            if not new_fields:
                break
            print(f"  [form] {len(new_fields)} new field(s) revealed after fill — handling")
            for field in new_fields:
                already_seen.add((field.label_text, field.selector))
                value = map_field_to_profile(field, user_profile, job_data)
                if value == "__SKIP__":
                    continue
                if value is None and field.label_text:
                    value = answer_question(
                        question=field.label_text,
                        field_type=field.field_type,
                        options=field.options,
                        user_profile=user_profile,
                        job_data=job_data,
                        model=model,
                    )
                if value is not None:
                    await fill_field(page, field, value)
                    all_answers[field.label_text or field.name] = value

        # Final pass — look for any visible field-level validation errors
        # (Indeed shows them as <div> with "Date format is invalid", "Choose
        # an option to continue", etc.) and try to fix the offending input.
        try:
            invalid_reports = await page.evaluate("""() => {
                const errs = [];
                document.querySelectorAll('input[aria-invalid="true"], select[aria-invalid="true"]').forEach(el => {
                    // Find the nearest visible error text container
                    let txt = '';
                    let walker = el;
                    for (let i = 0; i < 6 && walker; i++) {
                        walker = walker.parentElement;
                        if (!walker) break;
                        const err = walker.querySelector('[id*="error-text"], [data-testid*="error"], [class*="error"]');
                        if (err && err.textContent.trim()) {
                            txt = err.textContent.trim();
                            break;
                        }
                    }
                    errs.push({
                        name: el.name || '',
                        id: el.id || '',
                        placeholder: el.placeholder || '',
                        type: el.type || el.tagName.toLowerCase(),
                        value: el.value || '',
                        error: txt
                    });
                });
                return errs;
            }""")
            if invalid_reports:
                from datetime import date as _date
                print(f"  [form] {len(invalid_reports)} field(s) still invalid after fill — attempting repair")
                for inv in invalid_reports:
                    is_date_field = (
                        inv.get("type") == "date"
                        or "mm/dd/yyyy" in (inv.get("placeholder") or "").lower()
                        or "date format" in (inv.get("error") or "").lower()
                    )
                    if is_date_field:
                        # A stale max/min-date validator (Indeed caches "today" a
                        # day behind, so a "Today's Date" answer of the real today
                        # trips "Answer must not be after 07/01/2026"). If the error
                        # names a boundary date, honor THAT date; else use today.
                        import re as _re_dt
                        err_txt = inv.get("error") or ""
                        m_dt = _re_dt.search(r"(\d{1,2}/\d{1,2}/\d{4})", err_txt)
                        fill_val = m_dt.group(1) if m_dt else _date.today().strftime("%m/%d/%Y")
                        # Use attribute selectors — Indeed ids contain colons
                        # (e.g. ":r3i:") which break `#id` syntax without escaping.
                        if inv["id"]:
                            safe_id = inv["id"].replace('"', '\\"')
                            sel = f'[id="{safe_id}"]'
                        else:
                            safe_name = (inv["name"] or "").replace('"', '\\"')
                            sel = f'[name="{safe_name}"]'
                        try:
                            el = await page.query_selector(sel)
                            if el:
                                await el.click(timeout=2000)
                                await el.fill("")
                                await el.fill(fill_val)
                                await human_delay(0.3, 0.6)
                                print(f"    [repair] {inv['name']}: re-filled with {fill_val}"
                                      + (" (boundary from error)" if m_dt else " (today)"))
                        except Exception as e:
                            print(f"    [repair] {inv['name']}: {e}")
                    elif "shorter than" in (inv.get("error") or "").lower() or "must be" in (inv.get("error") or "").lower() and "character" in (inv.get("error") or "").lower():
                        import re as _re
                        err = inv.get("error") or ""
                        m = _re.search(r"(\d+)\s*character", err)
                        cap = int(m.group(1)) if m else 75
                        # Find this field's label via DOM and re-prompt LLM with the cap.
                        try:
                            name = inv.get("name") or ""
                            safe_name = name.replace('"', '\\"')
                            label_text = await page.evaluate(
                                "(n) => {"
                                "  const el = document.querySelector('[name=\"' + n + '\"]');"
                                "  if (!el) return '';"
                                "  if (el.labels && el.labels[0]) return el.labels[0].textContent.trim();"
                                "  if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');"
                                "  return '';"
                                "}",
                                safe_name,
                            )
                            if label_text:
                                shorter = answer_question(
                                    question=label_text + f" (Be brief — under {cap} characters total)",
                                    field_type="text",
                                    options=[],
                                    user_profile=user_profile,
                                    job_data=job_data,
                                    model=model,
                                    max_length=cap,
                                )
                                if shorter:
                                    sel = f'[name="{safe_name}"]'
                                    el = await page.query_selector(sel)
                                    if el:
                                        await el.click(timeout=2000)
                                        await el.fill("")
                                        await el.fill(str(shorter)[:cap])
                                        await human_delay(0.3, 0.6)
                                        print(f"    [repair] {name}: re-filled with shorter answer ({len(str(shorter))}c, cap {cap})")
                        except Exception as e:
                            print(f"    [repair] length-error fix failed: {e}")
                    elif "choose an option" in (inv.get("error") or "").lower():
                        # An unfilled required radio/select. EEO self-ID groups
                        # (veteran/disability/race) have no "Yes" option, so pick a
                        # safe decline/negative self-ID first; then fall back to a
                        # "Yes" option (agreement/opt-in), then a single option.
                        name = inv.get("name") or ""
                        try:
                            picked = await page.evaluate("""(name) => {
                                if (!name) return false;
                                const radios = [...document.querySelectorAll(
                                    'input[type="radio"][name="' + name.replace(/"/g, '\\\\"') + '"]'
                                )];
                                if (!radios.length) return false;
                                const lblOf = r => (r.labels && r.labels[0])
                                    ? r.labels[0].textContent.trim().toLowerCase() : '';
                                const pick = r => { r.click(); r.checked = true;
                                    r.dispatchEvent(new Event('change', {bubbles:true})); return true; };
                                // 1) EEO / decline-style safe defaults
                                const prefs = ['not a protected veteran', 'wish to answer',
                                    'decline to', 'prefer not', 'do not have a disability'];
                                for (const p of prefs) {
                                    const r = radios.find(x => lblOf(x).includes(p));
                                    if (r) return pick(r);
                                }
                                // 2) Yes (agreement / opt-in)
                                const y = radios.find(x => lblOf(x) === 'yes');
                                if (y) return pick(y);
                                // 3) single-option group
                                if (radios.length === 1) return pick(radios[0]);
                                return false;
                            }""", name)
                            if picked:
                                await human_delay(0.3, 0.6)
                                print(f"    [repair] {name}: picked a safe option for unfilled required radio")
                            elif name:
                                # Not a radio group — it's a mosaic single-select
                                # combobox (<li role=option>). Reuse the combobox
                                # filler with a safe negative answer ("No" maps to
                                # None/decline via _yesno_option).
                                sel = f'[data-testid="input-{name}-select-list-select-list"]'
                                cb_el = await page.query_selector(sel)
                                if not cb_el:
                                    sel = f'[data-testid*="{name}"][role="combobox"]'
                                    cb_el = await page.query_selector(sel)
                                if cb_el:
                                    fld = FormField(field_type="combobox", selector=sel,
                                                    label_text=name)
                                    ok = await _fill_combobox(page, fld, "No")
                                    print(f"    [repair] {name}: combobox repair {'ok' if ok else 'failed'}")
                                else:
                                    print(f"    [warn] invalid field {name} ({inv.get('error')}) — no radio or combobox found")
                            else:
                                print(f"    [warn] invalid field {name} ({inv.get('error')}) — no matching option to pick")
                        except Exception as e:
                            print(f"    [repair] {name}: {e}")
                    else:
                        print(f"    [warn] invalid field {inv.get('name')} ({inv.get('error') or 'no error text'}) — no repair rule")
        except Exception as e:
            print(f"  [form] error scan failed: {e}")

        # Click Continue to go to the next step
        if not await click_continue(page):
            # If no Continue button, maybe we're on the last step with Submit
            if await is_review_page(page):
                continue  # Loop back to handle review page
            print("  [form] Cannot advance, form may be stuck")
            return "failed", all_answers

    print(f"  [form] Exceeded {max_steps} form steps, giving up")
    return "failed", all_answers
