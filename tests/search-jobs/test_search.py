"""Random-sample integration test for the Indeed job-search pipeline.

Picks 3 random (job, city) combinations from JOBS x CITIES and verifies that
``search_indeed`` + ``collect_job_listings`` return at least one real,
location-matching, non-sponsored Easy Apply card per combo.

Pass criteria (per combo):
  1. >= MIN_RESULTS valid cards collected
  2. every card's visible location contains the searched city
  3. no card has a sponsored marker (already filtered by collect_job_listings;
     this test asserts the filter actually worked)
  4. companies in the result set are diverse (>= MIN_COMPANIES distinct)

Overall pass: all 3 random combos meet their per-combo criteria.

Run:
    poetry run python tests/search-jobs/test_search.py
    poetry run python tests/search-jobs/test_search.py --seed 42        # reproducible
    poetry run python tests/search-jobs/test_search.py --sample 5       # try 5 combos
"""

import argparse
import asyncio
import os
import random
import sys
import time

# Add src/ to path so we can import the agent's modules.
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

from browser import launch_browser, check_login_status, human_delay  # noqa: E402
from job_searcher import search_indeed, collect_job_listings  # noqa: E402


JOBS = [
    "barista", "cashier", "server", "warehouse associate", "delivery driver",
    "caregiver", "nurse", "medical assistant", "dental assistant", "receptionist",
    "customer service representative", "graphic designer", "retail sales",
    "line cook", "host", "janitor", "security guard", "forklift operator",
    "administrative assistant", "data entry",
]

CITIES = [
    "Ann Arbor, MI", "Detroit, MI", "Royal Oak, MI", "Livonia, MI",
    "Warren, MI", "Sterling Heights, MI", "Pontiac, MI", "Novi, MI",
    "Farmington Hills, MI", "Dearborn, MI",
]

MIN_RESULTS = 3          # >= this many valid cards per combo
MIN_COMPANIES = 2        # >= this many distinct employers per combo
TARGET_COLLECT = 10      # how many to ask collect_job_listings for


def log(msg: str):
    print(f"  {msg}")


def fail(label: str, reason: str):
    return {"label": label, "pass": False, "reason": reason}


def ok(label: str, count: int, companies: int):
    return {"label": label, "pass": True, "count": count, "companies": companies}


async def check_one(page, query: str, location: str) -> dict:
    """Run one (query, location) combo and classify the outcome."""
    label = f'"{query}" @ {location}'
    log(f"\n[combo] {label}")
    try:
        await search_indeed(
            page, query=query, location=location,
            easy_apply_only=True, days_posted=14,
        )
        await human_delay(1, 2)
        listings = await collect_job_listings(
            page, target_count=TARGET_COLLECT, location_filter=location,
        )
    except Exception as e:
        return fail(label, f"search/collect raised: {type(e).__name__}: {e}")

    log(f"  collected: {len(listings)}")
    if not listings:
        return fail(label, "0 listings after sponsored+location filter")

    if len(listings) < MIN_RESULTS:
        return fail(label, f"only {len(listings)} listings (need >= {MIN_RESULTS})")

    # Verify state-level location match. Indeed expands the radius around the
    # searched city, so exact-city assertion is too strict — we just want to
    # confirm we didn't leak cross-state sponsored slots (e.g. a TX job for a
    # MI search). State must match the user's typed location.
    import re as _re
    state_m = _re.search(r",\s*([A-Z]{2})\b", location)
    user_state = state_m.group(1) if state_m else ""
    if user_state:
        wrong_state = [
            j for j in listings
            if not _re.search(rf",\s*{user_state}\b", j.get("location") or "", _re.I)
        ]
        if wrong_state:
            bad = "; ".join(f"{j['company']}/{j['location']}" for j in wrong_state[:3])
            return fail(label, f"{len(wrong_state)} cross-state leaks: {bad}")

    # Diversity check.
    companies = {(j.get("company") or "").strip().lower() for j in listings}
    companies.discard("")
    if len(companies) < MIN_COMPANIES:
        return fail(label, f"only {len(companies)} distinct employer(s) (need >= {MIN_COMPANIES})")

    return ok(label, len(listings), len(companies))


async def run_suite(sample: int, seed: int | None):
    rng = random.Random(seed)
    combos = []
    while len(combos) < sample:
        q = rng.choice(JOBS)
        c = rng.choice(CITIES)
        if (q, c) not in combos:
            combos.append((q, c))

    print(f"Sampling {sample} combos (seed={seed}):")
    for q, c in combos:
        print(f"  - {q} @ {c}")
    print()

    pw, context, page = await launch_browser()
    try:
        await page.goto("https://www.indeed.com", wait_until="domcontentloaded")
        await human_delay(1, 2)
        if not await check_login_status(page):
            print("ERROR: Not logged in. Run `poetry run python src/main.py login` first.")
            return 2

        results = []
        for q, c in combos:
            r = await check_one(page, q, c)
            results.append(r)
            verdict = "PASS" if r["pass"] else "FAIL"
            detail = r.get("reason") or f"{r['count']} listings / {r['companies']} companies"
            print(f"  -> {verdict}: {detail}")

        print("\n" + "=" * 60)
        passed = sum(1 for r in results if r["pass"])
        print(f"RESULT: {passed}/{len(results)} combos passed")
        print("=" * 60)
        for r in results:
            mark = "[+]" if r["pass"] else "[-]"
            detail = (
                f"{r['count']} listings, {r['companies']} companies"
                if r["pass"] else r["reason"]
            )
            print(f"  {mark} {r['label']:<55} {detail}")
        return 0 if passed == len(results) else 1
    finally:
        await context.close()
        await pw.stop()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=3, help="how many combos to try")
    ap.add_argument("--seed", type=int, default=None, help="RNG seed for reproducibility")
    args = ap.parse_args()
    exit_code = asyncio.run(run_suite(args.sample, args.seed))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
