"""Smart profile data manager for job applications.

Maintains a persistent user_profile.yaml with all personal data needed to fill
Indeed Easy Apply forms. Tracks completeness and interactively collects only
missing datums.
"""

import os
import re
import yaml
from datetime import date

PROFILE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "user_profile.yaml")

# Each datum: key -> {prompt, type, required, category}
DATUMS = {
    # Personal info
    "first_name":       {"prompt": "First name", "type": "str", "required": True, "category": "Personal"},
    "last_name":        {"prompt": "Last name", "type": "str", "required": True, "category": "Personal"},
    "email":            {"prompt": "Email address", "type": "str", "required": True, "category": "Personal"},
    "phone":            {"prompt": "Phone number", "type": "str", "required": True, "category": "Personal"},
    "street_address":   {"prompt": "Street address (e.g. '123 Main St')", "type": "str", "required": True, "category": "Personal"},
    "city":             {"prompt": "City", "type": "str", "required": True, "category": "Personal"},
    "state":            {"prompt": "State (abbreviation, e.g. NY)", "type": "str", "required": True, "category": "Personal"},
    "zip_code":         {"prompt": "ZIP code", "type": "str", "required": True, "category": "Personal"},
    # home_address is NOT collected: it is DERIVED from the parts above by
    # sync_derived_fields (street, city, "STATE ZIP"). The agent pulls the parts
    # OR the combined home_address depending on the form field.

    # Work authorization
    "work_authorized_us": {"prompt": "Authorized to work in the US? (yes/no)", "type": "bool", "required": True, "category": "Work Auth"},
    "sponsorship_needed": {"prompt": "Will you require visa sponsorship? (yes/no)", "type": "bool", "required": True, "category": "Work Auth"},
    "us_citizen": {"prompt": "Are you a U.S. citizen? (yes/no) — some jobs require it; answered truthfully, never assumed", "type": "bool", "required": True, "category": "Work Auth"},

    # Experience
    "years_experience":   {"prompt": "Total years of professional experience", "type": "int", "required": True, "category": "Experience"},
    "experience_summary": {"prompt": "Brief experience summary (2-3 sentences)", "type": "str", "required": True, "category": "Experience"},
    "current_job_title":  {"prompt": "Current/most recent job title", "type": "str", "required": True, "category": "Experience"},

    # Education
    "highest_education": {"prompt": "Highest education level (High School / Associate / Bachelor's / Master's / PhD)", "type": "str", "required": True, "category": "Education"},
    "degree_field":      {"prompt": "Degree field (e.g. Computer Science)", "type": "str", "required": True, "category": "Education"},

    # Optional but valuable
    "current_employer":   {"prompt": "Current/most recent employer", "type": "str", "required": False, "category": "Experience"},
    "school_name":        {"prompt": "School/university name", "type": "str", "required": False, "category": "Education"},
    "graduation_year":    {"prompt": "Graduation year", "type": "str", "required": False, "category": "Education"},
    "skills":             {"prompt": "Key skills (comma-separated, e.g. Python, AWS, SQL)", "type": "list", "required": False, "category": "Skills"},
    "certifications":     {"prompt": "Certifications (comma-separated, or 'none')", "type": "list", "required": False, "category": "Skills"},
    "desired_salary":     {"prompt": "Desired salary (e.g. 80000 or 80000-100000)", "type": "str", "required": False, "category": "Preferences"},
    "start_date":         {"prompt": "When can you start? (e.g. Immediately, 2 weeks, a date)", "type": "str", "required": False, "category": "Preferences"},
    "willing_to_relocate": {"prompt": "Willing to relocate? (yes/no)", "type": "bool", "required": False, "category": "Preferences"},
    "work_type_preference": {"prompt": "Work type preference (remote / hybrid / onsite / any)", "type": "str", "required": False, "category": "Preferences"},
    "linkedin_url":       {"prompt": "LinkedIn URL (or 'none')", "type": "str", "required": False, "category": "Personal"},

    # NOTE: no default-resume datum by design. A fresh resume is tuned per job
    # (resume_tuner.build_tuned_resume); we never upload a static default PDF.

    # Common screening defaults
    "has_drivers_license":          {"prompt": "Do you have a valid driver's license? (yes/no)", "type": "bool", "required": False, "category": "Screening"},
    "has_reliable_transportation":  {"prompt": "Do you have reliable transportation? (yes/no)", "type": "bool", "required": False, "category": "Screening"},
    "can_pass_background_check":    {"prompt": "Can you pass a background check? (yes/no)", "type": "bool", "required": False, "category": "Screening"},
    "can_pass_drug_test":           {"prompt": "Can you pass a drug test? (yes/no)", "type": "bool", "required": False, "category": "Screening"},
    "is_18_or_older":               {"prompt": "Are you 18 years or older? (yes/no)", "type": "bool", "required": False, "category": "Screening"},

    # Demographics (used by voluntary self-identification questions on many job
    # applications). All optional — if left blank, the LLM falls back to a
    # reasonable default and the field can still be answered manually.
    "gender":          {"prompt": "Gender (Male / Female / Non-binary / Decline to identify)", "type": "str", "required": False, "category": "Demographics"},
    "pronouns":        {"prompt": "Pronouns (e.g. She/Her, He/Him, They/Them, or 'decline')", "type": "str", "required": False, "category": "Demographics"},
    "ethnicity":       {"prompt": "Race / ethnicity (e.g. 'White (Not Hispanic or Latino)', 'Black or African American', 'Asian', 'Hispanic or Latino', 'Two or More Races', 'Decline to identify')", "type": "str", "required": False, "category": "Demographics"},
    "hispanic_latino": {"prompt": "Are you Hispanic or Latino? (yes/no)", "type": "bool", "required": False, "category": "Demographics"},
    "veteran_status":  {"prompt": "Veteran status (Protected veteran / Not a protected veteran / Decline to identify)", "type": "str", "required": False, "category": "Demographics"},
    "disability_status": {"prompt": "Disability status (Yes / No / Decline to identify)", "type": "str", "required": False, "category": "Demographics"},
    "date_of_birth":   {"prompt": "Date of birth (MM/DD/YYYY) — optional, used when forms require it", "type": "str", "required": False, "category": "Demographics"},

    # Preferences (additional)
    "desired_hourly_rate": {"prompt": "Desired hourly rate (e.g. 20 or 18-25) — optional, leave blank if salary is preferred", "type": "str", "required": False, "category": "Preferences"},
    "availability":        {"prompt": "Availability (e.g. 'Monday-Friday, 8am-5pm' or 'flexible')", "type": "str", "required": False, "category": "Preferences"},

    # Current/most-recent employment details (used by Indeed forms that ask
    # for an employment-history block).
    "current_employer_city":  {"prompt": "City where your current/most-recent employer is located", "type": "str", "required": False, "category": "Experience"},
    "current_employer_state": {"prompt": "State (abbreviation) where your current/most-recent employer is located", "type": "str", "required": False, "category": "Experience"},
    "current_job_start_date": {"prompt": "Start date for current/most-recent job (MM/DD/YYYY)", "type": "str", "required": False, "category": "Experience"},
    "currently_employed":     {"prompt": "Are you currently employed at this job? (yes/no)", "type": "bool", "required": False, "category": "Experience"},
}


def _parse_value(raw: str, dtype: str):
    """Convert a raw input string to the appropriate Python type."""
    raw = raw.strip()
    if not raw:
        return None

    if dtype == "bool":
        return raw.lower() in ("yes", "y", "true", "1")
    elif dtype == "int":
        try:
            return int(raw)
        except ValueError:
            print(f"  Invalid number, please try again.")
            return None
    elif dtype == "list":
        if raw.lower() == "none":
            return []
        return [item.strip() for item in raw.split(",") if item.strip()]
    else:
        return raw


def load_profile() -> dict:
    """Load the user profile from YAML. Returns empty dict if file doesn't exist."""
    if not os.path.exists(PROFILE_PATH):
        return {}
    with open(PROFILE_PATH, "r") as f:
        data = yaml.safe_load(f) or {}
    return data


# Canonical work-history schema: profile["work_history"] is a list of jobs,
# MOST RECENT FIRST, each a dict with any of these string keys:
#   job_title, employer, location ("City, State"), start_date, end_date, summary
# It is the source of truth for employment history; the flat "current_*" fields
# below are derived from work_history[0] so form_filler (which reads the flat
# fields) needs no knowledge of the list.
WORK_HISTORY_KEYS = ("job_title", "employer", "location", "start_date", "end_date", "summary")


def _parse_month(s: str):
    """Parse 'MM/YYYY' or 'YYYY' -> (year, month). Open/present/blank -> None."""
    s = (s or "").strip().lower()
    if s in ("", "present", "current", "now", "ongoing"):
        return None
    m = re.search(r"(\d{1,2})[/-](\d{4})", s)
    if m:
        return (int(m.group(2)), int(m.group(1)))
    m = re.search(r"\b(\d{4})\b", s)
    if m:
        return (int(m.group(1)), 12)
    return None


def sync_derived_fields(profile: dict) -> dict:
    """Backfill the flat 'current job' fields + employer_locations from work_history.

    work_history is the source of truth for employment history. The "current"
    job is the one with the latest end date (an open/'present'/future end counts
    as most current), NOT list order.

    Also derives the combined home_address from the individual street/city/state/
    zip parts, so the agent can fill either a full-address field or a part field.
    """
    # --- Combined home address from parts (always) ---
    street = (profile.get("street_address") or "").strip()
    city = (profile.get("city") or "").strip()
    state = (profile.get("state") or "").strip()
    zc = str(profile.get("zip_code") or "").strip()
    if street or city or state or zc:
        tail = f"{state} {zc}".strip()
        profile["home_address"] = ", ".join(s for s in (street, city, tail) if s)

    # --- Current-job fields from work_history ---
    jobs = [j for j in (profile.get("work_history") or []) if isinstance(j, dict) and j]
    if not jobs:
        return profile

    def end_key(j):
        p = _parse_month(j.get("end_date"))
        return (9999, 12) if p is None else p  # open-ended -> most current

    top = max(jobs, key=end_key)
    if top.get("job_title"):
        profile["current_job_title"] = top["job_title"]
    if top.get("employer"):
        profile["current_employer"] = top["employer"]
    if top.get("start_date"):
        profile["current_job_start_date"] = top["start_date"]

    end_p = _parse_month(top.get("end_date"))
    today = (date.today().year, date.today().month)
    profile["currently_employed"] = (end_p is None) or (end_p >= today)

    loc = (top.get("location") or "").strip()
    if "," in loc:
        city, state = (p.strip() for p in loc.split(",", 1))
        if city:
            profile["current_employer_city"] = city
        if state:
            profile["current_employer_state"] = state
    elif loc:  # single token like "Remote"
        profile["current_employer_city"] = loc
        profile["current_employer_state"] = ""

    # Rebuild employer_locations FRESH from work_history (drops stale entries),
    # used by form_filler's resume-review card fixer.
    locs = {j["employer"]: j["location"] for j in jobs
            if j.get("employer") and j.get("location")}
    if locs:
        profile["employer_locations"] = locs
    return profile


def save_profile(profile: dict):
    """Write the user profile to YAML (deriving flat fields from work_history first)."""
    sync_derived_fields(profile)
    os.makedirs(os.path.dirname(PROFILE_PATH), exist_ok=True)
    with open(PROFILE_PATH, "w") as f:
        yaml.dump(profile, f, default_flow_style=False, sort_keys=False)


def check_completeness(profile: dict = None) -> tuple[int, int, list[str]]:
    """Check how many required datums are filled.

    Returns:
        (filled_count, total_required, list_of_missing_keys)
    """
    if profile is None:
        profile = load_profile()

    required_keys = [k for k, v in DATUMS.items() if v["required"]]
    total = len(required_keys)
    missing = []

    for key in required_keys:
        val = profile.get(key)
        if val is None or val == "" or val == []:
            missing.append(key)

    filled = total - len(missing)
    return filled, total, missing


def print_status(profile: dict = None):
    """Print a human-readable profile completeness report."""
    if profile is None:
        profile = load_profile()

    filled, total, missing = check_completeness(profile)

    if filled == total:
        print(f"\n  Profile status: {filled}/{total} required datums filled — good to go!")
    else:
        print(f"\n  Profile status: {filled}/{total} required datums filled")
        print(f"  Missing: {', '.join(missing)}")

    # Count optional datums
    optional_keys = [k for k, v in DATUMS.items() if not v["required"]]
    optional_filled = sum(1 for k in optional_keys if profile.get(k) not in (None, "", []))
    print(f"  Optional datums: {optional_filled}/{len(optional_keys)} filled")


def collect_missing(required_only=True):
    """Interactively ask for missing datums. Saves after each answer.

    Args:
        required_only: If True, only ask for required datums. If False, ask for all missing.
    """
    profile = load_profile()

    if required_only:
        keys_to_check = [k for k, v in DATUMS.items() if v["required"]]
    else:
        keys_to_check = list(DATUMS.keys())

    missing = [k for k in keys_to_check if profile.get(k) in (None, "", [])]

    if not missing:
        print("\n  All datums are filled!")
        return profile

    print(f"\n  Need to collect {len(missing)} datum(s):\n")

    for key in missing:
        datum = DATUMS[key]
        category = datum["category"]
        prompt_text = datum["prompt"]

        while True:
            raw = input(f"  [{category}] {prompt_text}: ").strip()
            if not raw:
                print("    (skipped)")
                break

            value = _parse_value(raw, datum["type"])
            if value is not None:
                profile[key] = value
                save_profile(profile)
                break

    return profile


def collect_all():
    """Interactively collect ALL missing datums (required + optional)."""
    profile = load_profile()
    print_status(profile)

    filled, total, missing_required = check_completeness(profile)

    if missing_required:
        print("\n  Let's fill in the required datums first:\n")
        profile = collect_missing(required_only=True)

    # Ask if user wants to fill optional too
    optional_keys = [k for k, v in DATUMS.items() if not v["required"]]
    optional_missing = [k for k in optional_keys if profile.get(k) in (None, "", [])]

    if optional_missing:
        print(f"\n  {len(optional_missing)} optional datums remaining.")
        answer = input("  Fill optional datums too? (yes/no): ").strip().lower()
        if answer in ("yes", "y"):
            profile = collect_missing(required_only=False)

    print_status(profile)
    return profile


def get_profile() -> dict:
    """Load and return the full profile dict."""
    return load_profile()
