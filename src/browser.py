"""Playwright-based browser automation for Indeed.

Handles launching a persistent Chromium session (preserving Indeed login),
navigating pages, and providing human-like delays. Uses the same persistent
profile pattern as the Facebook Marketplace agent.
"""

import os
import asyncio
import random
import time
from playwright.async_api import async_playwright, Page, BrowserContext

PROFILE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "browser_profile")
DEBUG_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "debug")
INDEED_URL = "https://www.indeed.com"

# Toggle by main.py via --debug; when True, dump_page also fires at non-miss checkpoints
DEBUG_MODE = False


async def launch_browser(headless=False) -> tuple:
    """Launch a persistent Chromium browser with a saved profile (keeps your Indeed login)."""
    os.makedirs(PROFILE_DIR, exist_ok=True)
    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=headless,
        viewport={"width": 1280, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = context.pages[0] if context.pages else await context.new_page()
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
        print(f"  [debug] dumped: {html_path}")
        print(f"  [debug] dumped: {png_path}")
    except Exception as e:
        print(f"  [debug] dump failed for '{label}': {e}")
        return None
    return html_path


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
