"""Diagnostics: dump what the browser actually sees on selected vacancies pages.

SITES env var: '|'-separated substrings of university names, or 'ZERO' for every site that
returned no research posts / an error in data/coverage.csv. Output: debug/<slug>.json
"""
import asyncio
import csv
import json
import os
import re
from pathlib import Path

from playwright.async_api import async_playwright

import collector as C

OUT = C.ROOT / "debug"


async def dump(ctx, uni, sem):
    url = uni["vacancies_url"] or uni["landing_url"]
    slug = re.sub(r"[^a-z0-9]+", "-", uni["name"].lower()).strip("-")
    rec = {"name": uni["name"], "url": url}
    async with sem:
        page = None
        try:
            page = await C.open_page(ctx, url)
            await page.wait_for_timeout(3000)
            rec["final_url"] = page.url
            rec["title"] = await page.title()
            rec["text"] = (await page.evaluate("() => document.body ? document.body.innerText : ''"))[:6000]
            anchors = await page.evaluate(C.EXTRACT_JS)
            rec["anchors"] = [{"t": a["title"][:120], "h": a["href"][:250]} for a in anchors][:400]
            rec["frames"] = [f.url for f in page.frames][:10]
            rec["buttons"] = await page.evaluate(
                "() => [...document.querySelectorAll('button, input[type=submit], [role=button]')]"
                ".map(b => (b.innerText || b.value || b.getAttribute('aria-label') || '').trim()).filter(Boolean).slice(0, 60)")
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {str(e).splitlines()[0][:300]}"
        finally:
            if page:
                await page.close()
    (OUT / f"{slug}.json").write_text(json.dumps(rec, indent=1, ensure_ascii=False))
    print("dumped", uni["name"], rec.get("error", ""), flush=True)


async def main():
    OUT.mkdir(exist_ok=True)
    unis = list(csv.DictReader(open(C.UNIS_CSV, newline="", encoding="utf-8")))
    sites = os.environ.get("SITES", "ZERO")
    if sites == "ZERO":
        cov = {r["university"]: r for r in csv.DictReader(open(C.DATA / "coverage.csv"))}
        unis = [u for u in unis if u["name"] in cov and (cov[u["name"]]["status"] != "ok" or cov[u["name"]]["research_links"] == "0")]
    else:
        keys = [s.strip().lower() for s in sites.split("|") if s.strip()]
        unis = [u for u in unis if any(k in u["name"].lower() for k in keys)]
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(user_agent=C.UA, locale="en-GB", viewport={"width": 1366, "height": 900})
        sem = asyncio.Semaphore(8)
        await asyncio.gather(*(dump(ctx, u, sem) for u in unis))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
