"""LIVE-submit ONE Indeed Easy Apply flow, end to end. Clicks Submit for real.

User-authorized real submission (see /apply-iteration-loop). This is the DEFAULT
runner: full e2e including the live submit. It tunes a resume for the job itself
(pass a PDF to reuse one). Safety is the live gate in fill_and_submit_application
(sanity + resume-integrity), which blocks submit on any wrong/ungrounded answer or
un-grounded resume -- not a dry pass.

Usage: python tools/apply_live.py <job_url> [resume_pdf]
  resume_pdf: optional; reuse a tuned PDF across retries of the same job. Omit to
              tune one (the path prints as RESUME_PDF=... to pass back).
"""
import sys, os, asyncio, json

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, SRC)

import browser, form_filler
browser.DEBUG_MODE = True      # still dump pages for the audit trail
form_filler.DRY_RUN = False    # LIVE: click Submit at the gate
from browser import launch_browser, check_login_status, human_delay
from job_searcher import navigate_to_job, extract_job_details
from form_filler import fill_and_submit_application
from search_profiles import effective_profile
from resume_tuner import build_tuned_resume
import field_inventory
from field_inventory import UNHANDLED_REQUIRED_PATH


async def main(job_url, resume_pdf=None):
    if os.path.exists(UNHANDLED_REQUIRED_PATH):
        os.remove(UNHANDLED_REQUIRED_PATH)
    field_inventory.init_run(mode="live")
    profile = effective_profile()   # user profile + active search overrides (salary, etc.)
    pw, ctx, page = await launch_browser()

    gqldir = os.path.join("data", "debug", "gql")
    os.makedirs(gqldir, exist_ok=True)
    n = {"i": 0}
    async def _on_response(resp):
        try:
            if "apis.indeed.com/graphql" not in resp.url:
                return
            body = await resp.text()
            try: post = resp.request.post_data or ""
            except Exception: post = ""
            try: op = json.loads(post).get("operationName", "") or "op"
            except Exception: op = "op"
            n["i"] += 1
            try: req_obj = json.loads(post)
            except Exception: req_obj = post
            try: resp_obj = json.loads(body)
            except Exception: resp_obj = body
            with open(os.path.join(gqldir, f"{n['i']:03d}-{op[:40]}.json"), "w", encoding="utf-8") as fh:
                json.dump({"url": resp.url, "status": resp.status, "request": req_obj, "response": resp_obj}, fh, indent=2, default=str)
        except Exception:
            pass
    page.on("response", lambda r: asyncio.ensure_future(_on_response(r)))
    try:
        await page.goto("https://www.indeed.com", wait_until="domcontentloaded")
        await human_delay(1, 2)
        if not await check_login_status(page):
            print("RESULT: status=NOT_LOGGED_IN  |  unhandled_required=0"); return
        await navigate_to_job(page, job_url)
        details = await extract_job_details(page)
        job = {**details, "job_url": job_url}
        print(f"[apply_live] {job.get('title')} @ {job.get('company')}")

        if resume_pdf and os.path.exists(resume_pdf):
            resume = resume_pdf
            print(f"[apply_live] reusing resume: {resume}")
        else:
            print("[apply_live] tuning resume (once)...")
            resume = build_tuned_resume(job, profile)
            print(f"[apply_live] RESUME_PDF={resume}")

        status, answers = await fill_and_submit_application(page, profile, job, resume)

        reqs = []
        if os.path.exists(UNHANDLED_REQUIRED_PATH):
            reqs = [json.loads(l) for l in open(UNHANDLED_REQUIRED_PATH, encoding="utf-8")]

        print("\n" + "=" * 64)
        print(f"RESULT: status={status}  |  unhandled_required={len(reqs)}")
        print("=" * 64)
        if reqs:
            print("UNHANDLED REQUIRED FIELDS:")
            for r in reqs:
                opts = f"  options={r['options']}" if r.get("options") else ""
                print(f"  - [{r['type']}] {r['label']!r}{opts}")
        print("\nFILLED (label = value):")
        for k, v in answers.items():
            print(f"  {str(k)[:48]!r} = {str(v)[:60]!r}")

        # Durable record of what we actually submitted (only on a real submit).
        if status == "applied":
            try:
                from application_record import save_application
                rec_path = save_application(job, answers, resume, status)
                print(f"[apply_live] application record: {rec_path}")
            except Exception as e:
                print(f"[apply_live] WARNING: could not write application record: {e}")
    finally:
        field_inventory.flush()
        await ctx.close()
        await pw.stop()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python tools/apply_live.py <job_url> [resume_pdf]")
    url = sys.argv[1]
    rp = sys.argv[2] if len(sys.argv) > 2 else None
    if rp and not os.path.exists(rp):
        sys.exit(f"resume not found: {rp}")
    asyncio.run(main(url, rp))
