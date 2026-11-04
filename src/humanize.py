"""Human-like Playwright interactions to feed reCAPTCHA Enterprise behavioral signals.

Our captured smartapply CreatePreview request showed keystroke count `asilom=0` and
interaction count `asiloh=1` — the apply flow emitted almost no human-input telemetry
(teleport clicks, `fill()` instead of keystrokes, no wheel events). reCAPTCHA
Enterprise (enterprise.js / BotGuard) scores the whole session, so these helpers add
real mouse movement, key-by-key typing, and wheel scrolls to the token's window.

Presence and timing of events matter more than curve shape (Akrout 2019), so this
stays simple. ponytail: one module, random+asyncio only; every helper falls back to
the plain Playwright call on any error so a humanization bug can never block an apply.
"""
import asyncio
import random
import math
from playwright.async_api import Page

ENABLED = True
# Playwright doesn't expose the cursor position, so we remember it ourselves.
_last = {"x": 640.0, "y": 360.0}


async def _sleep(a, b):
    await asyncio.sleep(random.uniform(a, b))


async def _move_to(page: Page, x: float, y: float):
    """Move the real mouse to (x,y) along a jittered, eased, multi-step path."""
    x0, y0 = _last["x"], _last["y"]
    dist = math.hypot(x - x0, y - y0)
    steps = max(6, min(28, int(dist / 18)))  # ~18px/step, Fitts-ish
    for i in range(1, steps + 1):
        t = i / steps
        ease = t * t * (3 - 2 * t)           # smoothstep
        await page.mouse.move(x0 + (x - x0) * ease + random.uniform(-2, 2),
                              y0 + (y - y0) * ease + random.uniform(-2, 2))
        await asyncio.sleep(random.uniform(0.004, 0.012))
    await page.mouse.move(x, y)
    _last["x"], _last["y"] = x, y


async def _point_in(page: Page, loc):
    """A random point inside the element's box, after scrolling it into view."""
    try:
        await loc.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    box = await loc.bounding_box()
    if not box:
        return None
    return (box["x"] + box["width"] * random.uniform(0.3, 0.7),
            box["y"] + box["height"] * random.uniform(0.35, 0.65))


async def click(page: Page, loc):
    """Move the cursor to the element and click with a real 60-130ms press."""
    if not ENABLED:
        await loc.click()
        return
    try:
        p = await _point_in(page, loc)
        if p is None:
            await loc.click()
            return
        await _move_to(page, *p)
        await _sleep(0.05, 0.18)            # settle / hover
        await page.mouse.down()
        await _sleep(0.06, 0.13)            # press duration
        await page.mouse.up()
    except Exception:
        try:
            await loc.click()
        except Exception:
            await page.evaluate("e => e.click()", loc)


async def type_text(page: Page, loc, text):
    """Focus via a real click, clear any prefill, then type key-by-key with cadence."""
    text = "" if text is None else str(text)
    if not ENABLED:
        await loc.fill("")
        await loc.fill(text)
        return
    try:
        await click(page, loc)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        for ch in text:
            await page.keyboard.type(ch)
            d = random.lognormvariate(math.log(0.07), 0.5)   # mostly 40-110ms
            await asyncio.sleep(min(0.4, max(0.03, d)))
        await _sleep(0.15, 0.4)
    except Exception:
        try:
            await loc.fill("")
            await loc.fill(text)
        except Exception:
            pass


async def browse(page: Page, reads: int = 2):
    """Ambient human entropy before a scored moment (Continue/Submit): a little wheel
    scrolling, idle pointer drift, and reading pauses, so the token window carries real
    mouse/scroll events instead of only teleport clicks."""
    if not ENABLED:
        return
    try:
        for _ in range(reads):
            await page.mouse.wheel(0, random.randint(120, 380))
            await _sleep(0.4, 1.1)
            await _move_to(page, _last["x"] + random.uniform(-120, 120),
                           _last["y"] + random.uniform(-80, 80))
        if random.random() < 0.5:
            await page.mouse.wheel(0, -random.randint(80, 200))
            await _sleep(0.3, 0.7)
    except Exception:
        pass


def _selfcheck():
    """Geometry + key-delay distribution sanity (no browser needed)."""
    import statistics
    assert 6 <= max(6, min(28, int(500 / 18))) <= 28
    ds = [min(0.4, max(0.03, random.lognormvariate(math.log(0.07), 0.5)))
          for _ in range(5000)]
    assert all(0.03 <= d <= 0.4 for d in ds)
    assert 0.05 <= statistics.mean(ds) <= 0.2, statistics.mean(ds)
    print("humanize selfcheck ok; mean key delay", round(statistics.mean(ds), 3))


if __name__ == "__main__":
    _selfcheck()
