"""Fast, autonomous reCAPTCHA v3 score probe to A/B browser-launch configs.

Loads a public reCAPTCHA v3 score page (antcpt) with OUR launch_browser() and
prints the score it reports. This is a generic v3 score, NOT Indeed's Enterprise
trustScoreHash, but it's a cheap directional signal: if our automation config
scores ~0.1 while a real human scores ~0.9, the automation layer is the problem.

Usage: python tools/score_probe.py [url]
"""
import sys, os, asyncio, re

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, SRC)
import browser
from browser import launch_browser, human_delay

SCORE_PAGE = sys.argv[1] if len(sys.argv) > 1 else "https://antcpt.com/score_detector/"


async def main():
    pw, ctx, page = await launch_browser()
    scores = []

    async def on_resp(resp):
        try:
            if "antcpt.com" in resp.url and resp.request.method == "POST":
                t = await resp.text()
                print(f"ANTCPT_POST {resp.url} -> {t[:200]}")
                for m in re.findall(r'"score"\s*:\s*(0?\.\d+|[01](?:\.0+)?)', t):
                    scores.append(("net", float(m)))
        except Exception:
            pass
    page.on("response", lambda r: asyncio.ensure_future(on_resp(r)))

    try:
        await page.goto(SCORE_PAGE, wait_until="domcontentloaded")
        await human_delay(4, 6)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        await human_delay(3, 5)
        body = ""
        try:
            body = await page.inner_text("body")
        except Exception:
            pass
        for m in re.findall(r'[Ss]core[^0-9]{0,20}(0?\.\d+|[01](?:\.0+)?)', body):
            scores.append(("dom", float(m)))
        # report the automation banner / webdriver as a sanity check on the config
        tells = await page.evaluate("""() => ({
            webdriver: navigator.webdriver,
            hasPwInit: typeof window.__pwInitScripts !== 'undefined',
            webglRenderer: (() => { try {
                const c = document.createElement('canvas').getContext('webgl');
                const e = c.getExtension('WEBGL_debug_renderer_info');
                return c.getParameter(e.UNMASKED_RENDERER_WEBGL);
            } catch (e) { return 'n/a'; } })(),
            ua: navigator.userAgent,
        })""")
        print("TELLS:", tells)
        if scores:
            vals = [v for _, v in scores]
            print(f"SCORES_SEEN: {scores}")
            print(f"SCORE_MIN={min(vals)} SCORE_MAX={max(vals)}")
        else:
            print("SCORE: none parsed (check the page manually / try another url)")
    finally:
        await ctx.close()
        await pw.stop()


if __name__ == "__main__":
    asyncio.run(main())
