"""Search profiles: the "what to apply to" criteria, stored structured under
user_data/search_profiles/<slug>.json and mapped onto the apply bot's criteria
(roles/location/salary_min/remote_preference). The active one is named in
user_data/active_search. Shared by the GUI (user_fill_info) and apply_preflight.
"""

import datetime
import json
import os
import re

import profile_manager as pm

SEARCH_DIR = os.path.join(pm.USER_DATA_DIR, "search_profiles")
ACTIVE_SEARCH = os.path.join(pm.USER_DATA_DIR, "active_search")


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s[:60] or "search"


def _file(name: str) -> str:
    return os.path.join(SEARCH_DIR, name + ".json")


def load_search(name: str) -> dict:
    try:
        with open(_file(name), encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return {}


def list_searches() -> list:
    if not os.path.isdir(SEARCH_DIR):
        return []
    out = []
    for f in sorted(os.listdir(SEARCH_DIR)):
        if f.endswith(".json"):
            name = f[:-5]
            d = load_search(name)
            dt = datetime.datetime.fromtimestamp(os.path.getmtime(_file(name)))
            out.append({"name": name, "title": d.get("title", name),
                        "updated": f"{dt.month}/{dt.day}/{dt.year % 100:02d}"})
    return out


def save_search(data: dict, orig: str = None) -> str:
    """Write a search profile keyed by slug(title); drop the old file if retitled."""
    os.makedirs(SEARCH_DIR, exist_ok=True)
    name = _slug(data.get("title"))
    with open(_file(name), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    if orig and orig != name and os.path.exists(_file(orig)):
        os.remove(_file(orig))
        if active_search() == orig:
            set_active_search(name)
    return name


# Overlap between the two profiles: search-profile key -> user-profile key it
# overrides. The search profile is the "what to apply to" criteria, so where it
# sets a value that the user profile also carries, the SEARCH value wins. Salary
# lives here, not on the user profile (desired pay is per-search, not per-person).
OVERRIDES = {"salary_min": "desired_salary"}


def effective_profile(name: str = None) -> dict:
    """User profile overlaid with the active search profile's overlapping values.

    Search takes priority (OVERRIDES). Used by the apply flow so salary-expectation
    fields reflect THIS search's floor rather than a stale user-profile number.
    """
    prof = dict(pm.load_profile())
    sname = name or active_search()
    search = load_search(sname) if sname else {}
    for skey, pkey in OVERRIDES.items():
        val = search.get(skey)
        if val not in (None, "", []):
            prof[pkey] = val
    return prof


def active_search() -> str:
    try:
        return open(ACTIVE_SEARCH, encoding="utf-8").read().strip()
    except OSError:
        return ""


def set_active_search(name: str):
    os.makedirs(pm.USER_DATA_DIR, exist_ok=True)
    with open(ACTIVE_SEARCH, "w", encoding="utf-8") as f:
        f.write(name.strip())
