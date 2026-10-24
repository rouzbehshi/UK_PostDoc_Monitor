"""One-off probes to decide how to read specific recruitment systems. Output: debug/probe.json"""
import asyncio
import json

from playwright.async_api import async_playwright

import collector as C

PAGES = [
    "https://www.jobs.ac.uk/search/employer/university-of-oxford",
    "https://www.jobs.ac.uk/search/employer/the-university-of-manchester",
    "https://www.jobs.ac.uk/search/employer/university-college-london",
    "https://www.jobs.ac.uk/search/employer/ucl",
    "https://www.jobs.ac.uk/search/employer/university-of-glasgow",
    "https://www.jobs.ac.uk/search/?keywords=research&sortOrder=1&pageSize=100&organisationNameFacet%5B0%5D=university-of-oxford",
]
APIS = [
    "https://elxw.fa.em3.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList&finder=findReqs;siteNumber=CX_1001,limit=25,offset=0,sortBy=POSTING_DATES_DESC",
    "https://jobs.citystgeorges.ac.uk/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList&finder=findReqs;siteNumber=CX_1001,limit=25,offset=0",
]
NET = ["https://jobs.lse.ac.uk/V2/Vacancy", "https://www.vacancies.st-andrews.ac.uk/V2/Vacancy"]


async def main():
    out = {}
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(user_agent=C.UA, locale="en-GB", viewport={"width": 1366, "height": 900})
        for u in PAGES:
            try:
                pg = await C.open_page(ctx, u)
                a = await pg.evaluate(C.EXTRACT_JS)
                out[u] = {"final": pg.url, "title": await pg.title(),
                          "text": (await pg.evaluate("() => document.body.innerText"))[:2500],
                          "jobs": [x for x in a if "/job/" in x["href"]][:40]}
                await pg.close()
            except Exception as e:
                out[u] = {"error": str(e)[:300]}
        for u in APIS:
            try:
                r = await ctx.request.get(u, timeout=30000)
                out[u] = {"status": r.status, "body": (await r.text())[:3000]}
            except Exception as e:
                out[u] = {"error": str(e)[:300]}
        for u in NET:
            caps = []
            pg = await ctx.new_page()
            async def on_resp(resp):
                ct = resp.headers.get("content-type", "")
                if "json" in ct:
                    try:
                        caps.append({"url": resp.url, "body": (await resp.text())[:2500]})
                    except Exception:
                        pass
            pg.on("response", on_resp)
            try:
                await pg.goto(u, timeout=45000)
                await pg.wait_for_timeout(8000)
                html = await pg.content()
                out[u] = {"final": pg.url, "json": caps[:8], "html_snip": html[html.find("View job") - 3000: html.find("View job") + 500] if "View job" in html else html[:2000]}
            except Exception as e:
                out[u] = {"error": str(e)[:300], "json": caps[:8]}
            await pg.close()
        await b.close()
    C.ROOT.joinpath("debug").mkdir(exist_ok=True)
    (C.ROOT / "debug" / "probe.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))


asyncio.run(main())
