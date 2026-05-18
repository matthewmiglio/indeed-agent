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
    ("disability status", "_no_disability"),
    ("have a disability", "_no_disability"),
    ("disability *", "_no_disability"),
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
    ("recent employer", "current_employer"),
    ("previous employer", "current_employer"),
    ("recent job title", "current_job_title"),
    ("most recent job title", "current_job_title"),
    ("previous job title", "current_job_title"),
    ("recent position", "current_job_title"),
    # Employment-history dates — fall through to None (LLM) when profile
    # lacks the data; we don't want to auto-fill today's date as a past
    # employment start date.
    ("start date", "current_job_start_date"),
    ("end date", "_end_date"),  # special — handled below

    # Education
    ("highest level of education", "highest_education"),
    ("education level", "highest_education"),
    ("highest degree", "highest_education"),
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
                return profile.get("disability_status") or "No, I do not have a disability and have not had one in the past"
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
            # Special case: full name
            if profile_key == "_full_name":
                first = profile.get("first_name", "")
                last = profile.get("last_name", "")
                full = f"{first} {last}".strip()
                return full if full else None

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

    # Substring match (target contained in option)
    for opt in options:
        if target_lower in opt.lower():
            return opt

    # Substring match (option contained in target)
    for opt in options:
        if opt.lower().strip() in target_lower:
            return opt

    # Yes/No special handling
    if target_lower in ("yes", "true"):
        for opt in options:
            if opt.lower().strip() in ("yes", "true", "y"):
                return opt
    if target_lower in ("no", "false"):
        for opt in options:
            if opt.lower().strip() in ("no", "false", "n"):
                return opt

    return None


async def click_apply_button(page: Page) -> bool:
    """Find and click the Easy Apply / Apply Now button on a job posting.

    Returns True if the apply form was opened successfully.
    """
    print("  [apply] Looking for Apply button...")

    # Try multiple selectors for the apply button
    selectors = [
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
            if btn and await btn.is_visible():
                print(f"  [apply] Found apply button: {selector}")
                await btn.click()
                await human_delay(2, 4)
                return True
        except Exception:
            continue

    print("  [apply] No Apply button found on page")
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


async def fill_and_submit_application(page: Page, user_profile: dict,
                                       job_data: dict, resume_path: str,
                                       model: str = "mistral",
                                       max_steps: int = 8) -> tuple[str, dict]:
    """Complete the entire Indeed Easy Apply flow.

    Handles multi-step forms by analyzing each page, filling fields,
    and clicking through until submission.

    Args:
        page: Playwright page with the job posting loaded.
        user_profile: Full user profile dict.
        job_data: Current job posting data for LLM context.
        resume_path: Path to resume PDF to upload.
        model: Ollama model name for LLM answering.
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

    for step in range(max_steps):
        print(f"\n  [form] === Step {step + 1}/{max_steps} ===")

        # Check if we've reached the review/submit page
        if await is_review_page(page):
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

        # Resume-source picker (Indeed offers "Use your Indeed Resume" vs
        # "Upload a resume"). Per user requirement, always pick Upload —
        # never the stored Indeed resume nor AI-tailored variant.
        if await _is_resume_source_picker(page):
            print("  [form] Resume-source picker detected — selecting 'Upload a resume'")
            picked = await _pick_upload_resume_radio(page)
            if picked:
                await human_delay(1, 2)
                # If a file input now appeared, upload the PDF.
                fi = await page.query_selector('input[type="file"]')
                if fi:
                    try:
                        await fi.set_input_files(resume_path)
                        all_answers["resume"] = resume_path
                        print(f"    [fill] Uploaded resume: {resume_path}")
                        # Give Indeed time to parse the upload and reveal Continue.
                        await human_delay(3, 5)
                    except Exception as e:
                        print(f"    [fill] Resume upload failed: {e}")
                # Always try to advance past the picker page; treat this step as
                # handled and skip field-analysis on the resume picker itself.
                await click_continue(page)
                continue

        # Analyze the current form page
        fields = await analyze_form_page(page)
        field_inventory.record_page(fields, job_url=page.url, step=step + 1)

        if not fields:
            print("  [form] No form fields found on this page")
            await dump_page(page, f"form-step-{step + 1}-no-fields", force=True)
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

            if field.current_value and field.field_type not in ("radio", "checkbox"):
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
                # with a stricter target. Mistral occasionally ignores the
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
                                await el.fill(_date.today().strftime("%m/%d/%Y"))
                                await human_delay(0.3, 0.6)
                                print(f"    [repair] {inv['name']}: re-filled with today's date")
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
                        # An unfilled required radio/select. If the group has a
                        # Yes option (or is named "terms"/"agree"/"consent"),
                        # picking Yes is the right safe default — it's almost
                        # always an agreement / opt-in question.
                        name = inv.get("name") or ""
                        try:
                            picked = await page.evaluate("""(name) => {
                                if (!name) return false;
                                const radios = document.querySelectorAll(
                                    'input[type="radio"][name="' + name.replace(/"/g, '\\\\"') + '"]'
                                );
                                if (!radios.length) return false;
                                // Find a radio whose label text is "Yes"
                                for (const r of radios) {
                                    const lbl = (r.labels && r.labels[0])
                                        ? r.labels[0].textContent.trim().toLowerCase()
                                        : '';
                                    if (lbl === 'yes') {
                                        r.click();
                                        r.checked = true;
                                        r.dispatchEvent(new Event('change', {bubbles:true}));
                                        return true;
                                    }
                                }
                                // Single-option group — just pick the first.
                                if (radios.length === 1) {
                                    radios[0].click();
                                    radios[0].checked = true;
                                    radios[0].dispatchEvent(new Event('change', {bubbles:true}));
                                    return true;
                                }
                                return false;
                            }""", name)
                            if picked:
                                await human_delay(0.3, 0.6)
                                print(f"    [repair] {name}: picked Yes for unfilled required radio")
                            else:
                                print(f"    [warn] invalid field {name} ({inv.get('error')}) — couldn't pick Yes")
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
