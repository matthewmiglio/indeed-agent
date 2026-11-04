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
from search_profiles import effective_profile
from resume_tuner import build_tuned_resume
import field_inventory
from field_inventory import UNHANDLED_REQUIRED_PATH


async def main(job_url, resume_pdf=None):
    if os.path.exists(UNHANDLED_REQUIRED_PATH):
        os.remove(UNHANDLED_REQUIRED_PATH)   # isolate this attempt
    field_inventory.init_run(mode="dry-run")
    profile = effective_profile()   # user profile + active search overrides (salary, etc.)
    pw, ctx, page = await launch_browser()

    # --- probe: log backend calls that carry the finish/submit application state ---
    KW = ("finishApplicationUrl", "finishApplication", "readyToSubmit",
          "submitApplication", "isITVCheckSuccessful", "FinishApply")
    netlog = os.path.join("data", "debug", "net-finish.log")
    gqldir = os.path.join("data", "debug", "gql")
    os.makedirs(gqldir, exist_ok=True)
    gqln = {"i": 0}
    async def _on_response(resp):
        try:
            u = resp.url
            if not any(s in u for s in ("graphql", "smartapply", "indeedapply", "apply")):
                return
            body = await resp.text()
            # dump every graphql request+response this run for offline analysis
            if "apis.indeed.com/graphql" in u:
                import json as _j
                try: post = resp.request.post_data or ""
                except Exception: post = ""
                opname = ""
                try: opname = _j.loads(post).get("operationName", "") or ""
                except Exception: pass
                gqln["i"] += 1
                fn = os.path.join(gqldir, f"{gqln['i']:03d}-{opname[:40] or 'op'}.json")
                try: req_obj = _j.loads(post)
                except Exception: req_obj = post
                try: resp_obj = _j.loads(body)
                except Exception: resp_obj = body
                with open(fn, "w", encoding="utf-8") as fh:
                    _j.dump({"url": u, "status": resp.status, "request": req_obj, "response": resp_obj}, fh, indent=2, default=str)
            if not any(k in body for k in KW):
                return
            # Full request+response for the submitApplication mutation (unique field
            # saveFileSuccess) — the request variables show whether the client asked
            # to preview vs finalize, and the response is the server's readiness view.
            if "apis.indeed.com/graphql" in u and "saveFileSuccess" in body:
                fullp = os.path.join("data", "debug", "net-submit-full.json")
                if not os.path.exists(fullp):
                    try:
                        req = resp.request
                        post = req.post_data or ""
                    except Exception:
                        post = ""
                    import json as _j
                    try: resp_obj = _j.loads(body)
                    except Exception: resp_obj = body
                    try: req_obj = _j.loads(post)
                    except Exception: req_obj = post
                    with open(fullp, "w", encoding="utf-8") as fh:
                        _j.dump({"request": req_obj, "response": resp_obj}, fh, indent=2, default=str)
            with open(netlog, "a", encoding="utf-8") as fh:
                fh.write(f"\n==== {resp.status} {u}\n")
                for k in KW:
                    i = body.find(k)
                    if i >= 0:
                        fh.write(f"  ...{body[max(0,i-80):i+200]}...\n")
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
