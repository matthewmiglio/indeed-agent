"""Resolve + validate the active user profile and active search profile for the
apply loop. Prints the resolved criteria, or exits non-zero with a clear reason
if either is unset or too thin.

  python tools/apply_preflight.py          # validate + print criteria
  python tools/apply_preflight.py --check   # offline self-check of the rules

"Too thin":
  profile  -> no profile data, or any REQUIRED datum is missing.
  search   -> no roles listed, or in-person with no location given.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import profile_manager as pm  # noqa: E402
import search_profiles as sp  # noqa: E402


def validate(profile: dict, completeness: tuple, search_name: str, search: dict) -> dict:
    """Pure validation. Raises ValueError (thin/unset) or returns resolved criteria."""
    pname = pm.active_profile()
    if not profile:
        raise ValueError(f"No active user profile. Selected '{pname}' has no data. "
                         "Pick/fill a profile in the GUI (Profiles tab).")
    filled, total, missing = completeness
    if missing:
        raise ValueError(f"User profile '{pname}' is too thin: missing required field(s): "
                         f"{', '.join(missing)}. Fill them in the GUI (Profiles tab).")

    if not search_name:
        raise ValueError("No search profile selected. Pick one in the GUI (Apply tab).")
    if not search:
        raise ValueError(f"Selected search profile '{search_name}' has no data or is missing.")
    roles = [r for r in (search.get("roles") or []) if str(r).strip()]
    if not roles:
        raise ValueError(f"Search profile '{search.get('title', search_name)}' is too thin: "
                         "no roles listed. Add at least one role in the GUI (Apply tab).")
    location = (search.get("location") or "").strip()
    remote = (search.get("remote_preference") or "any").lower()
    if remote == "onsite" and not location:
        raise ValueError(f"Search profile '{search.get('title', search_name)}' is in-person "
                         "but has no location. Add a location or change remote preference.")
    return {"profile": pname, "profile_filled": filled, "profile_total": total,
            "search": search.get("title", search_name),
            "roles": roles, "location": location, "remote": remote,
            "salary_min": search.get("salary_min") or None}


def preflight() -> dict:
    profile = pm.load_profile()
    completeness = pm.check_completeness(profile)
    sname = sp.active_search()
    return validate(profile, completeness, sname, sp.load_search(sname) if sname else {})


def _selfcheck():
    ok_profile = {"first_name": "A"}
    complete = (40, 40, [])
    thin_complete = (38, 40, ["email", "phone"])
    search = {"title": "200k SWE", "roles": ["Software Engineer"], "location": "", "remote_preference": "remote"}

    def expect_fail(msg_substr, *args):
        try:
            validate(*args)
        except ValueError as e:
            assert msg_substr in str(e), f"wrong msg: {e}"
            return
        raise AssertionError(f"expected failure containing {msg_substr!r}")

    expect_fail("no data", {}, complete, "s", search)
    expect_fail("too thin: missing required", ok_profile, thin_complete, "s", search)
    expect_fail("No search profile selected", ok_profile, complete, "", {})
    expect_fail("no roles listed", ok_profile, complete, "s", {"roles": []})
    expect_fail("in-person but has no location", ok_profile, complete, "s",
                {"roles": ["X"], "remote_preference": "onsite", "location": ""})
    crit = validate(ok_profile, complete, "s", search)
    assert crit["roles"] == ["Software Engineer"] and crit["remote"] == "remote"
    print("selfcheck OK")


if __name__ == "__main__":
    if "--check" in sys.argv:
        _selfcheck()
        sys.exit(0)
    try:
        c = preflight()
    except ValueError as e:
        print(f"PREFLIGHT FAIL: {e}")
        sys.exit(1)
    print("PREFLIGHT OK")
    print(f"PROFILE: {c['profile']} ({c['profile_filled']}/{c['profile_total']} required filled)")
    print(f"SEARCH:  {c['search']}")
    print(f"ROLES:   {', '.join(c['roles'])}")
    print(f"LOCATION:{' ' + c['location'] if c['location'] else ' (remote / any)'}")
    print(f"REMOTE:  {c['remote']}")
    print(f"SALARY_FLOOR: {c['salary_min'] or 'none'}")
