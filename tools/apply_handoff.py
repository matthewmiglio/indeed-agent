"""Bot fills the whole Indeed Easy-Apply flow, then hands off to YOU for the final Submit.

The bot never clicks Submit (DRY_RUN stays on). It fills every field, uploads the
tuned resume, reaches the review page, then leaves the browser OPEN so you can click
Submit by hand. Close the browser window when you're done to end the run.

Usage: python tools/apply_handoff.py <job_url> [resume_pdf]

Also logs apis.indeed.com/graphql responses to data/debug/gql/ so if your click DOES
go through we can see whether a finishApplicationUrl finally appears.
"""
import sys, os, asyncio, json

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, SRC)

import browser, form_filler
browser.DEBUG_MODE = True
form_filler.DRY_RUN = True          # the BOT never submits; the human does
from browser import launch_browser, check_login_status, human_delay
from job_searcher import navigate_to_job, extract_job_details
from form_filler import fill_and_submit_application
from search_profiles import effective_profile
from resume_tuner import build_tuned_resume


async def main(job_url, resume_pdf=None):
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
            post = ""
            try: post = resp.request.post_data or ""
            except Exception: pass
            try: op = json.loads(post).get("operationName", "") or "op"
            except Exception: op = "op"
            n["i"] += 1
            with open(os.path.join(gqldir, f"{n['i']:03d}-{op[:40]}.json"), "w", encoding="utf-8") as fh:
                try: req_obj = json.loads(post)
                except Exception: req_obj = post
                try: resp_obj = json.loads(body)
                except Exception: resp_obj = body
                json.dump({"url": resp.url, "status": resp.status, "request": req_obj, "response": resp_obj}, fh, indent=2, default=str)
        except Exception:
            pass
    page.on("response", lambda r: asyncio.ensure_future(_on_response(r)))

    try:
        await page.goto("https://www.indeed.com", wait_until="domcontentloaded")
        await human_delay(1, 2)
        if not await check_login_status(page):
            print("RESULT: NOT_LOGGED_IN — run: python src/main.py login"); return
        await navigate_to_job(page, job_url)
        details = await extract_job_details(page)
        job = {**details, "job_url": job_url}
        print(f"[handoff] {job.get('title')} @ {job.get('company')}")

        if resume_pdf and os.path.exists(resume_pdf):
            resume = resume_pdf
            print(f"[handoff] reusing resume: {resume}")
        else:
            print("[handoff] tuning resume (once)...")
            resume = build_tuned_resume(job, profile)
            print(f"[handoff] RESUME_PDF={resume}")

        status, answers = await fill_and_submit_application(page, profile, job, resume)
        print("\n" + "=" * 64)
        print(f"[handoff] bot finished the flow (status={status}). YOUR TURN:")
        print("  -> Click the Submit button in the browser window.")
        print("  -> If Submit is greyed out / does nothing, that's the server block.")
        print("  -> Close the browser window when done to end this run.")
        print("=" * 64)
        # Keep the browser open until you close it (whichever page is foremost).
        try:
            await ctx.pages[-1].wait_for_event("close", timeout=0)
        except Exception:
            # fallback: hold for 15 min if the close event can't bind
            await asyncio.sleep(900)
    finally:
        try: await ctx.close()
        except Exception: pass
        await pw.stop()


if __name__ == "__main__":
    url = sys.argv[1]
    rp = sys.argv[2] if len(sys.argv) > 2 else None
    asyncio.run(main(url, rp))
