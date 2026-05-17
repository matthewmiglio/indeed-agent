"""Top-level agent orchestrator that ties all modules together.

Flow: parse user prompt -> check profile -> search Indeed -> collect jobs ->
score each job -> Easy Apply to passing jobs -> persist to SQLite -> summary.
"""

import json
import os
import random
import time
from datetime import datetime

from prompt_parser import parse_prompt
from browser import launch_browser, human_delay, check_login_status
from job_searcher import search_indeed, collect_job_listings, extract_job_details, navigate_to_job
from job_scorer import score_job
from form_filler import fill_and_submit_application
from profile_manager import load_profile, check_completeness
from storage import get_db, save_job, save_application, save_session, is_already_applied
from distance import filter_jobs_by_commute, geocode
import field_inventory


def safe_print(text: str):
    """Print with Unicode chars replaced to avoid Windows console crashes."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))


def log(msg: str):
    """Print an indented agent status message."""
    safe_print(f"  >> {msg}")


def timestamp():
    """Return the current wall-clock time as HH:MM:SS for log lines."""
    return time.strftime("%H:%M:%S")


async def run_agent(user_prompt: str, resume_path: str = None, model: str = "mistral", dry_run: bool = False):
    """Run the full agent pipeline for a single user request.

    Steps:
        1. Parse the natural-language prompt into a structured intent.
        2. Verify profile completeness and resolve resume path.
        3. Launch the browser and search Indeed.
        4. Collect job listings from search results.
        5. Visit each job — extract details, score with LLM.
        6. Apply to passing jobs via Easy Apply.
        7. Print a summary and persist everything to SQLite.

    Args:
        user_prompt: The raw request string from the user.
        resume_path: Optional path to resume PDF (overrides profile default).
        model: Ollama model name used by all LLM modules.
    """
    run_start = time.time()
    field_inventory.init_run(mode="dry-run" if dry_run else "live")
    print(f"\n{'='*60}")
    print(f"[{timestamp()}] Agent started{' (DRY RUN)' if dry_run else ''}")
    print(f"{'='*60}")

    # Step 1: Parse the prompt
    print(f"\n--- Step 1: Parse prompt ---")
    t0 = time.time()
    intent = parse_prompt(user_prompt, model=model)
    print(f"[{timestamp()}] Parsing took {time.time() - t0:.1f}s total")

    job_title = intent["job_title"]
    location = intent.get("location")
    quantity = intent["quantity"]
    loc_str = f", in {location}" if location else ""
    log(f"Plan: search for '{job_title}'{loc_str}, apply to {quantity} jobs")
    if intent.get("exclusions"):
        log(f"Exclusions: {intent['exclusions']}")

    # Step 2: Load profile and resolve resume
    print(f"\n--- Step 2: Check profile + resume ---")
    profile = load_profile()
    filled, total, missing = check_completeness(profile)
    if missing:
        log(f"WARNING: Profile incomplete ({filled}/{total}). Missing: {', '.join(missing)}")
        log("Some form fields may not be fillable. Consider running 'python src/main.py profile'.")

    # Resolve resume path: CLI arg > intent > profile > None
    resume = resume_path or intent.get("resume_path") or profile.get("resume_path")
    if resume and os.path.exists(resume):
        log(f"Resume: {resume}")
    else:
        log("WARNING: No resume file found. Applications may fail on resume upload.")
        resume = None

    # Step 3: Launch browser and search
    print(f"\n--- Step 3: Launch browser + search ---")
    t0 = time.time()
    pw, context, page = await launch_browser()
    print(f"[{timestamp()}] Browser launched in {time.time() - t0:.1f}s")
    db = get_db()

    try:
        # Check login
        await page.goto("https://www.indeed.com", wait_until="domcontentloaded")
        await human_delay(1, 2)
        if not await check_login_status(page):
            log("ERROR: Not logged into Indeed. Run 'python src/main.py login' first.")
            return

        t0 = time.time()
        job_type = intent.get("job_type")
        if job_type == "any":
            job_type = None

        await search_indeed(
            page,
            query=job_title,
            location=location,
            job_type=job_type,
            easy_apply_only=True,
        )
        print(f"[{timestamp()}] Search page loaded in {time.time() - t0:.1f}s")

        # Step 4: Collect job listings
        print(f"\n--- Step 4: Collect job listings ---")
        t0 = time.time()
        # Collect 5x target to account for scoring rejections, already-applied,
        # and per-company dedup (sponsored slots clustered at one employer).
        target_collect = min(quantity * 5, 30)
        listings = await collect_job_listings(
            page, target_count=target_collect, location_filter=location
        )
        print(f"[{timestamp()}] Collected {len(listings)} listings in {time.time() - t0:.1f}s")
        # Easy Apply restriction is enforced at the URL level
        # (`sc=0kf:attr(DSQF7);` in search_indeed). The per-card text badge is
        # unreliable across SERP variants, so no client-side hard-filter here —
        # if a listing turns out not to be Easy Apply, click_apply_button will
        # bail with a clean "no Apply button" and we move on.

        for i, job in enumerate(listings):
            safe_print(f"  [{i+1}] {job.get('title', '?')} @ {job.get('company', '?')}")

        # Step 4.5: Distance filter (if commute limit specified)
        max_commute = intent.get("max_commute_minutes")
        too_far_count = 0
        if max_commute is not None:
            print(f"\n--- Step 4.5: Distance filter (max {max_commute} min) ---")
            home_address = profile.get("home_address")
            if not home_address:
                city = profile.get("city", "")
                state = profile.get("state", "")
                zip_code = profile.get("zip_code", "")
                home_address = f"{city}, {state} {zip_code}".strip(", ")

            if not home_address:
                log("WARNING: No home address in profile. Skipping distance filter.")
            else:
                home_coords = geocode(home_address)
                if home_coords is None:
                    log("WARNING: Could not geocode home address. Skipping distance filter.")
                else:
                    log(f"Home: {home_address} -> ({home_coords[0]:.4f}, {home_coords[1]:.4f})")
                    listings, filtered_out = filter_jobs_by_commute(
                        listings, home_coords, max_commute
                    )
                    too_far_count = len(filtered_out)
                    for job in filtered_out:
                        job["status"] = "too_far"
                        save_job(db, job)
                    log(f"Distance filter: {len(listings)} passed, {too_far_count} too far")

        # Step 5: Score and apply
        print(f"\n--- Step 5: Score + apply ---")
        results = []
        applied_count = 0
        skipped_count = 0
        failed_count = 0
        errors = 0
        already_applied_count = 0

        applied_companies = set()
        for i, job in enumerate(listings):
            if applied_count >= quantity:
                log(f"Reached target of {quantity} applications, stopping.")
                break

            print(f"\n  --- Job {i+1}/{len(listings)} ---")
            safe_print(f"  Title: {job.get('title', '?')}")
            safe_print(f"  Company: {job.get('company', '?')}")

            try:
                # Skip if we've already applied to another role at this employer in
                # this run — sponsored Easy Apply slots from a single company tend
                # to dominate the SERP and would otherwise eat all 3 slots.
                company_key = (job.get("company") or "").strip().lower()
                if company_key and company_key in applied_companies:
                    log(f"Already applied to {job.get('company')} this run, skipping")
                    continue

                # Check if already applied
                if is_already_applied(db, job["job_url"]):
                    log("Already applied (in our database), skipping")
                    already_applied_count += 1
                    continue

                # Navigate to job detail page
                t0 = time.time()
                await navigate_to_job(page, job["job_url"])
                print(f"  [browser] Page loaded in {time.time() - t0:.1f}s")

                # Extract full details
                details = await extract_job_details(page)
                job.update(details)

                # Score the job
                score_result = score_job(job, profile, intent, model=model)
                job["score"] = score_result["score"]
                job["score_reasoning"] = score_result["reasoning"]

                if not score_result["pass"] and not dry_run:
                    job["status"] = "skipped"
                    save_job(db, job)
                    skipped_count += 1
                    results.append(job)
                    continue
                if dry_run and not score_result["pass"]:
                    log(f"Score: {score_result['score']}/100 — would-skip, but dry-run forces walk-through")

                # Apply!
                log(f"Score: {score_result['score']}/100 — PASS, applying...")
                field_inventory.record_job_start()

                status, answers = await fill_and_submit_application(
                    page, profile, job, resume, model=model
                )
                if status == "applied":
                    field_inventory.record_job_completed()
                elif status == "failed":
                    field_inventory.record_job_failed()

                job["status"] = status
                if status == "applied":
                    job["applied_at"] = datetime.now().isoformat()
                    applied_count += 1
                    if company_key:
                        applied_companies.add(company_key)

                job_id = save_job(db, job)

                if status == "applied":
                    save_application(db, job_id, resume or "", answers)
                    log(f"Successfully applied!")
                elif status == "already_applied":
                    already_applied_count += 1
                    log("Already applied on Indeed, skipping")
                else:
                    failed_count += 1
                    log(f"Application failed: {status}")

                results.append(job)

                # Delay between applications
                if applied_count < quantity:
                    delay = random.uniform(10, 30)
                    log(f"Waiting {delay:.0f}s before next application...")
                    await human_delay(delay * 0.8, delay * 1.2)

            except Exception as e:
                print(f"  [ERROR] {type(e).__name__}: {e}")
                job["status"] = "error"
                job["error_message"] = str(e)
                save_job(db, job)
                errors += 1
                results.append(job)
                continue

        # Step 6: Summary
        total_time = time.time() - run_start
        print(f"\n{'='*60}")
        print(f"[{timestamp()}] AGENT RUN COMPLETE — {total_time:.1f}s total")
        print(f"{'='*60}")
        print(f"  Jobs found:          {len(listings)}")
        print(f"  Applied:             {applied_count}")
        print(f"  Skipped (low score): {skipped_count}")
        print(f"  Already applied:     {already_applied_count}")
        if too_far_count:
            print(f"  Filtered (distance): {too_far_count}")
        print(f"  Failed:              {failed_count}")
        print(f"  Errors:              {errors}")
        print(f"{'='*60}")

        for r in results:
            title = r.get("title", "?")
            company = r.get("company", "?")
            score = r.get("score", "?")
            status = r.get("status", "?")
            safe_print(f"  [{status:>15}] {title} @ {company} (score: {score})")

        print(f"{'='*60}\n")

        # Save session
        summary = (f"Applied to {applied_count}/{quantity} jobs, "
                   f"{skipped_count} skipped, {failed_count} failed, "
                   f"{errors} errors in {total_time:.1f}s")
        save_session(db, user_prompt, json.dumps(intent), summary)

    finally:
        field_inventory.flush()
        await context.close()
        await pw.stop()
