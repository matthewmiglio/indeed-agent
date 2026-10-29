"""Dry-run ONE Indeed Easy Apply flow and report what blocks it. Never submits.

Usage: python tools/apply_iter.py <job_url> [resume_pdf]
  resume_pdf: reuse a tuned PDF across iterations of the same job (skips slow
              re-tuning). Omit on the first run to tune one; the path is printed
              as RESUME_PDF=... so you can pass it back.

Output ends with a machine-readable block:
  RESULT: status=<applied|failed|...>  |  unhandled_required=<N>
  UNHANDLED REQUIRED FIELDS ...   (label/type/options to map)
  FILLED (label = value) ...      (to eyeball wrong answers)
"""
import sys, os, asyncio, json

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, SRC)

import browser, form_filler
browser.DEBUG_MODE = True     # dump every form page to data/debug for HTML inspection
form_filler.DRY_RUN = True    # walk the flow, NEVER click Submit
from browser import launch_browser, check_login_status, human_delay
from job_searcher import navigate_to_job, extract_job_details
from form_filler import fill_and_submit_application
from profile_manager import load_profile
from resume_tuner import build_tuned_resume
import field_inventory
from field_inventory import UNHANDLED_REQUIRED_PATH


async def main(job_url, resume_pdf=None):
    if os.path.exists(UNHANDLED_REQUIRED_PATH):
        os.remove(UNHANDLED_REQUIRED_PATH)   # isolate this attempt
    field_inventory.init_run(mode="dry-run")
    profile = load_profile()
    pw, ctx, page = await launch_browser()
    try:
        await page.goto("https://www.indeed.com", wait_until="domcontentloaded")
        await human_delay(1, 2)
        if not await check_login_status(page):
            print("RESULT: status=NOT_LOGGED_IN  |  unhandled_required=0"); return
        await navigate_to_job(page, job_url)
        details = await extract_job_details(page)
        job = {**details, "job_url": job_url}
        print(f"[apply_iter] {job.get('title')} @ {job.get('company')}")

        if resume_pdf and os.path.exists(resume_pdf):
            resume = resume_pdf
            print(f"[apply_iter] reusing resume: {resume}")
        else:
            print("[apply_iter] tuning resume (once)...")
            resume = build_tuned_resume(job, profile)
            print(f"[apply_iter] RESUME_PDF={resume}")

        status, answers = await fill_and_submit_application(page, profile, job, resume)

        reqs = []
        if os.path.exists(UNHANDLED_REQUIRED_PATH):
            reqs = [json.loads(l) for l in open(UNHANDLED_REQUIRED_PATH, encoding="utf-8")]

        print("\n" + "=" * 64)
        print(f"RESULT: status={status}  |  unhandled_required={len(reqs)}")
        print("=" * 64)
        if reqs:
            print("UNHANDLED REQUIRED FIELDS (need a mapping / profile value):")
            for r in reqs:
                opts = f"  options={r['options']}" if r.get("options") else ""
                print(f"  - [{r['type']}] {r['label']!r}{opts}")
        print("\nFILLED (label = value):")
        for k, v in answers.items():
            print(f"  {str(k)[:48]!r} = {str(v)[:60]!r}")
    finally:
        field_inventory.flush()
        await ctx.close()
        await pw.stop()


if __name__ == "__main__":
    url = sys.argv[1]
    rp = sys.argv[2] if len(sys.argv) > 2 else None
    asyncio.run(main(url, rp))
