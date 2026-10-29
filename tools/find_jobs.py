"""Search Indeed for Easy Apply jobs and print candidate URLs. Read-only.

Usage: python tools/find_jobs.py "<title>" "<city, ST | remote>" [count]
Prints one line per job:  JOB\t<url>\t<title>\t<company>
Prints  NONE  if the search returned nothing (Indeed throttling / no results).
"""
import sys, os, asyncio

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, SRC)

from browser import launch_browser, check_login_status, human_delay
from job_searcher import search_indeed, collect_job_listings


async def main(title, location, n):
    loc = None if location.strip().lower() in ("", "none") else location
    pw, ctx, page = await launch_browser()
    try:
        await page.goto("https://www.indeed.com", wait_until="domcontentloaded")
        await human_delay(1, 2)
        if not await check_login_status(page):
            print("NOT_LOGGED_IN"); return
        await search_indeed(page, query=title, location=loc, job_type=None, easy_apply_only=True)
        listings = await collect_job_listings(page, target_count=n, location_filter=loc)
        printed = 0
        for job in listings:
            url = job.get("job_url") or ""
            if url:
                print(f"JOB\t{url}\t{job.get('title', '?')}\t{job.get('company', '?')}")
                printed += 1
        if not printed:
            print("NONE")
    finally:
        await ctx.close()
        await pw.stop()


if __name__ == "__main__":
    title = sys.argv[1]
    location = sys.argv[2] if len(sys.argv) > 2 else "remote"
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 8
    asyncio.run(main(title, location, n))
