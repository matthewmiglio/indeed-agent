"""Playwright-based browser automation for Indeed.

Handles launching a persistent Chromium session (preserving Indeed login),
navigating pages, and providing human-like delays. Uses the same persistent
profile pattern as the Facebook Marketplace agent.
"""

import os
import asyncio
import random
import time
from collections import deque
from playwright.async_api import async_playwright, Page, BrowserContext

PROFILE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "browser_profile")
DEBUG_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "debug")
INDEED_URL = "https://www.indeed.com"

# Toggle by main.py via --debug; when True, dump_page also fires at non-miss checkpoints
DEBUG_MODE = False

# Rolling capture of console messages + failed requests for the current browser,
# so a dump can show WHY a page misbehaved (dead JS, blocked request) and not just
# the final HTML. Reset each launch_browser(). ponytail: bounded deque, last N only.
_CONSOLE = deque(maxlen=400)
_NETFAIL = deque(maxlen=200)


def _attach_diagnostics(page: Page):
    """Record console output, uncaught JS errors, and failed requests on a page."""
    page.on("console", lambda m: _CONSOLE.append(f"{m.type}: {m.text[:300]}"))
    page.on("pageerror", lambda e: _CONSOLE.append(f"pageerror: {str(e)[:300]}"))
    page.on("requestfailed", lambda r: _NETFAIL.append(
        f"{(r.failure or '')[:80]} {r.method} {r.url[:160]}"))


async def launch_browser(headless=False) -> tuple:
    """Launch a persistent Chromium browser with a saved profile (keeps your Indeed login)."""
    os.makedirs(PROFILE_DIR, exist_ok=True)
    _CONSOLE.clear()
    _NETFAIL.clear()
    pw = await async_playwright().start()
    # Launch config hardened per the 20-agent research in
    # docs/browser_action_bot_score_improvemnts/. Key points proven there:
    #   - real Chrome (channel="chrome") is what reCAPTCHA scores; Chromium drifts.
    #   - DROP --enable-automation: it is what renders the "controlled by automated
    #     test software" banner and flips the AutomationControlled flag reCAPTCHA reads.
    #   - no_viewport=True: an emulated viewport makes screen vs outerWidth geometry
    #     impossible for a real monitor (a bot tell). Use the real OS window instead.
    #   - NO JS stealth init script: spoofing navigator.webdriver/plugins/WebGL from JS
    #     is net-NEGATIVE — it adds non-native getters that fail toString()/descriptor
    #     checks, a __pwInitScripts global, and a macOS WebGL string on a Windows box
    #     (cross-layer mismatch). Real Chrome's native values are already coherent.
    launch_kwargs = dict(
        headless=headless,
        no_viewport=True,
        ignore_default_args=["--enable-automation"],
        args=["--disable-blink-features=AutomationControlled"],
    )
    try:
        context = await pw.chromium.launch_persistent_context(
            PROFILE_DIR, channel="chrome", **launch_kwargs)
    except Exception as e:
        # Real Chrome is what scores well; the Chromium fallback has a drifted
        # fingerprint. Make the degrade LOUD rather than silently tanking the score.
        print(f"[browser] WARNING: real Chrome unavailable ({e}); "
              f"FALLING BACK TO CHROMIUM — expect a worse bot score")
        context = await pw.chromium.launch_persistent_context(PROFILE_DIR, **launch_kwargs)
    # Attach to the initial page and any page opened later (e.g. smartapply popups).
    context.on("page", _attach_diagnostics)
    page = context.pages[0] if context.pages else await context.new_page()
    _attach_diagnostics(page)
    return pw, context, page


async def login_session():
    """Open the browser to Indeed so the user can log in manually. Keeps session for future runs."""
    pw, context, page = await launch_browser(headless=False)
    await page.goto("https://secure.indeed.com/auth", wait_until="domcontentloaded")
    print("Log into Indeed in this browser window.")
    print("When you're done, close the browser to save your session.")
    try:
        await context.pages[0].wait_for_event("close", timeout=0)
    except Exception:
        pass
    await context.close()
    await pw.stop()
    print("Session saved.")


async def human_delay(min_s=1.0, max_s=3.0):
    """Random delay to look less robotic."""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def dump_page(page: Page, label: str, force: bool = False) -> str | None:
    """Save the current page HTML + a full-page screenshot for debugging.

    Files are written to ``data/debug/`` as ``{timestamp}-{label}.html`` and
    ``{timestamp}-{label}.png``. Returns the HTML path (or None if disabled).

    Args:
        page: Active Playwright page.
        label: Short slug (e.g. "search-results-empty"). Sanitized for filename.
        force: If True, dump regardless of DEBUG_MODE. Use at hard miss points.
    """
    if not (force or DEBUG_MODE):
        return None
    os.makedirs(DEBUG_DIR, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
    ts = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join(DEBUG_DIR, f"{ts}-{safe}")
    html_path = base + ".html"
    png_path = base + ".png"
    try:
        html = await page.content()
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(f"<!-- url: {page.url} -->\n")
            f.write(html)
        await page.screenshot(path=png_path, full_page=True)
        await _dump_diagnostics(page, base + ".log")
        print(f"  [debug] dumped: {html_path}")
        print(f"  [debug] dumped: {png_path}")
    except Exception as e:
        print(f"  [debug] dump failed for '{label}': {e}")
        return None
    return html_path


async def _dump_diagnostics(page: Page, path: str):
    """Write a sidecar .log answering WHY a page looks how it does: live hydration
    state (readyState, body text length, which key elements actually exist) plus the
    recent console errors and failed network requests the HTML can't show."""
    try:
        state = await page.evaluate(
            """() => ({
                readyState: document.readyState,
                bodyTextLen: (document.body && document.body.innerText || '').length,
                applyAnchor: !!document.querySelector('#primary-apply-action,[data-testid="viewjob-indeed-apply"],a[href*="smartapply.indeed.com"]'),
                applyButton: !!document.querySelector('button#indeedApplyButton,button[data-testid="indeedApplyButton"],button.indeed-apply-button'),
                descNew: !!document.querySelector('[data-testid="vj-job-description-heading"],.simple-job-description'),
                descOld: !!document.querySelector('#jobDescriptionText'),
            })"""
        )
    except Exception as e:
        state = {"error": f"evaluate failed (page may be gone): {e}"}
    errs = [c for c in _CONSOLE if c.startswith(("error", "warning", "pageerror"))]
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"url: {page.url}\n")
        for k, v in state.items():
            f.write(f"{k}: {v}\n")
        f.write(f"\n--- failed requests ({len(_NETFAIL)}) ---\n")
        f.write("\n".join(_NETFAIL) or "(none)")
        f.write(f"\n\n--- console errors/warnings ({len(errs)} of {len(_CONSOLE)} msgs) ---\n")
        f.write("\n".join(errs[-60:]) or "(none)")


async def screenshot(page: Page, label: str = "snapshot", full_page: bool = True) -> str:
    """Take a screenshot regardless of debug mode. Returns the file path."""
    os.makedirs(DEBUG_DIR, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(DEBUG_DIR, f"{ts}-{safe}.png")
    await page.screenshot(path=path, full_page=full_page)
    print(f"  [debug] screenshot: {path}")
    return path


async def check_login_status(page: Page) -> bool:
    """Check if we're still logged into Indeed (not redirected to login page)."""
    url = page.url.lower()
    if "secure.indeed.com/auth" in url or "/login" in url:
        return False
    return True
