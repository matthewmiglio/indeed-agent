"""Open a job's Indeed Easy Apply flow in a VISIBLE browser for manual walkthrough.

Usage: python tools/open_apply.py <job_url>

Uses your saved login profile. Clicks Apply to open the form, then leaves the window
open so you can go through it by hand. Does NOT auto-fill or submit anything.
Exits when you close the browser window.
"""
import sys, os, asyncio

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, SRC)

from browser import launch_browser, human_delay
from job_searcher import navigate_to_job
from form_filler import click_apply_button


async def main(job_url):
    pw, ctx, page = await launch_browser(headless=False)
    try:
        await navigate_to_job(page, job_url)
        await human_delay(1, 2)
        print("Opening the Easy Apply form...")
        await click_apply_button(page)
        print("\n=== Browser is open. Walk through the application by hand. ===")
        print("Upload your resume, reach 'Review your resume details', and open the")
        print("education / work-experience edit cards to see what info is missing.")
        print("Close the browser window when you're done — nothing is auto-filled or submitted.")
        try:
            await page.wait_for_event("close", timeout=0)  # stay open until you close it
        except Exception:
            pass
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
        await pw.stop()
    print("Browser closed.")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
