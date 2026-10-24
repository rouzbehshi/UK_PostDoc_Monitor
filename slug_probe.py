"""Find jobs.ac.uk employer slugs for institutions whose guessed slug failed. Output: debug/slugs.json"""
import asyncio, csv, json, re
from urllib.parse import quote_plus
from playwright.async_api import async_playwright
import collector as C

async def main():
    cov = {r["university"]: r for r in csv.DictReader(open(C.DATA / "coverage.csv"))}
    names = [n for n, r in cov.items() if r["jobsac_slug"] in ("", "employer not found") or r["jobsac_slug"].startswith("error")]
    out = {}
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(user_agent=C.UA)
        for n in names:
            q = re.sub(r"\(.*?\)", "", n).replace("&", "and").strip()
            r = await ctx.request.get(f"https://www.jobs.ac.uk/search/?keywords={quote_plus(q)}", timeout=30000)
            html = await r.text()
            slugs = sorted(set(re.findall(r"/search/employer/([a-z0-9-]+)", html)) | set(re.findall(r"organisationNameFacet[^=]*=([a-z0-9-]+)", html)))
            out[n] = slugs[:15]
            print(n, slugs[:8], flush=True)
        await b.close()
    C.ROOT.joinpath("debug").mkdir(exist_ok=True)
    (C.ROOT / "debug" / "slugs.json").write_text(json.dumps(out, indent=1))

asyncio.run(main())
