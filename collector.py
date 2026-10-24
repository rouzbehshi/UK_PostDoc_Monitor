"""
UK university research-job collector.

Every run:
  1. Opens each university's vacancies page (universities.csv) in headless Chromium,
     expands "show more"/next pages, and collects every link whose title looks like a
     research post (Research Associate/Assistant/Fellow, Postdoc, Researcher, ...).
  2. Compares with data/seen.json and keeps only postings never seen before.
  3. Opens each new posting and saves its full advert text.

Outputs (committed back to the repo by the GitHub Action):
  data/seen.json                 every research posting ever seen (key -> metadata)
  data/new/YYYY-MM-DD.json       today's new postings, with full advert text
  data/latest_new.json           copy of today's file (stable path for readers)
  data/open_research_jobs.csv    research postings currently listed on the sites
  data/coverage.csv              per-university diagnostics (did the page load? links found?)
  reports/YYYY-MM-DD.md          human-readable summary of the run
"""
import asyncio
import csv
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DATA_DIR", ROOT / "data"))
REPORTS = Path(os.environ.get("REPORTS_DIR", ROOT / "reports"))
UNIS_CSV = Path(os.environ.get("UNIS_CSV", ROOT / "universities.csv"))
TODAY = dt.date.today().isoformat()

CONCURRENCY = int(os.environ.get("CONCURRENCY", "6"))
PAGE_TIMEOUT_MS = 45_000
MAX_PAGES = 8            # pagination / "show more" clicks per site
MAX_TEXT = 20_000        # characters of advert text kept per posting
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
ONLY = os.environ.get("ONLY")  # optional: substring filter on university name, for testing

# Titles we keep. Deliberately broad on topic: the topic is judged later from the full text.
RESEARCH_RE = re.compile(
    r"\b(research\s+(associate|assistant|fellow|officer|scientist|engineer|software\s+engineer|"
    r"technician|staff|analyst|coordinator|manager|developer|lead|fellowship)|researcher|"
    r"post[\s-]?doc(toral)?|postdoctoral|research\s*/\s*teaching|ktp\s+associate|"
    r"(senior|junior|early[\s-]career|career\s+development|independent|chancellor'?s|leverhulme|"
    r"marie\s+curie|ukri|epsrc|future\s+leaders?)\s+(research\s+)?fellow|fellow\s+in\b|"
    r"\bRA\b|\bPDRA\b|\bPDRF\b)",
    re.I,
)
EXCLUDE_RE = re.compile(
    r"(studentship|phd\s+(student|position|opportunit|scholarship|project)|doctoral\s+(student|training|candidate)"
    r"|\bDTP\b|\bCDT\b|undergraduate|summer\s+(school|intern)|internship|honorary|visiting\s+student"
    r"|research\s+(degree|student)|clinical\s+research\s+nurse)",
    re.I,
)
GENERIC_LINK_TEXT = re.compile(r"^(view|more|details|read more|apply|apply now|more info|find out more|see more|view job|view details|job details)\b", re.I)
SHOW_MORE_RE = re.compile(r"^(show|load|view)\s+more|more\s+results|^next\b|^next\s+page|^[›»>]$", re.I)
VACANCY_LINK_RE = re.compile(r"(current\s+vacanc|search\s+(our\s+)?(jobs|vacanc)|view\s+(our\s+)?(current\s+)?(jobs|vacanc)|all\s+(jobs|vacanc)|job\s+search|browse\s+jobs|latest\s+vacanc|vacancies)", re.I)

# Words used only to sort the daily list (most relevant first); nothing is filtered on them.
PROFILE_TERMS = [
    "energy", "power system", "electricity", "battery", "storage", "bess", "grid", "market",
    "ancillary", "flexibility", "optimisation", "optimization", "stochastic", "forecast",
    "renewable", "net zero", "decarbon", "electrical", "smart grid", "demand response",
    "machine learning", "data science", "operational research", "operations research",
    "hydrogen", "electric vehicle", "distribution network", "transmission", "economics",
]

EXTRACT_JS = r"""
() => {
  const out = [];
  const generic = /^(view|more|details|read more|apply|apply now|more info|find out more|see more|view job|view details|job details)\b/i;
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href;
    if (!href || href.startsWith('javascript:') || href.startsWith('mailto:') || href.startsWith('tel:')) continue;
    let title = (a.innerText || a.getAttribute('aria-label') || a.title || '').replace(/\s+/g, ' ').trim();
    const box = a.closest('li, tr, article, [class*="job" i], [class*="vacanc" i], [class*="result" i], [class*="card" i]');
    const raw = box ? (box.innerText || '') : '';
    let ctx = raw.replace(/\s+/g, ' ').trim();
    if ((!title || title.length < 6 || generic.test(title)) && raw) {
      const parts = raw.split(/\n|\t|\|/).map(s => s.replace(/\s+/g, ' ').trim()).filter(s => s.length > 5 && !generic.test(s));
      title = (parts[0] || '').slice(0, 200);
    }
    if (ctx.length > 400) ctx = ctx.slice(0, 400);
    out.push({href, title: title.slice(0, 250), ctx});
  }
  return out;
}
"""

MARK_MORE_JS = r"""
(pattern) => {
  const re = new RegExp(pattern, 'i');
  document.querySelectorAll('[data-ukpm-more]').forEach(e => e.removeAttribute('data-ukpm-more'));
  const els = document.querySelectorAll('button, a, [role=button], input[type=button], input[type=submit]');
  for (const el of els) {
    const label = (el.innerText || el.value || el.getAttribute('aria-label') || el.title || '').replace(/\s+/g, ' ').trim();
    const rel = (el.getAttribute('rel') || '').toLowerCase();
    if (!(rel === 'next' || (label && label.length < 30 && re.test(label)))) continue;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    el.setAttribute('data-ukpm-more', '1');
    return true;
  }
  return false;
}
"""

TEXT_JS = r"""
() => {
  const pick = document.querySelector('main, [role=main], article, #content, .content, #main') || document.body;
  return (pick.innerText || document.body.innerText || '').replace(/\n{3,}/g, '\n\n').trim();
}
"""


def norm_url(u: str) -> str:
    """Normalise a job URL so the same posting gets the same key on every run."""
    p = urlparse(u)
    drop = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "pagestamp",
            "lastselectedfacet", "mode", "source", "gclid", "fbclid", "_gl", "sid", "jsessionid"}
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in drop]
    path = re.sub(r";jsessionid=[^/?]*", "", p.path, flags=re.I)
    return urlunparse((p.scheme, p.netloc.lower(), path.rstrip("/"), "", urlencode(q), ""))


def key_for(uni: str, url: str, title: str) -> str:
    return hashlib.sha1(f"{uni}|{norm_url(url)}".encode()).hexdigest()[:16]


def is_research(title: str, ctx: str) -> bool:
    if len(title) < 8 or len(title) > 250:
        return False
    if EXCLUDE_RE.search(title):
        return False
    return bool(RESEARCH_RE.search(title))


def score(text: str) -> int:
    t = text.lower()
    return sum(t.count(w) for w in PROFILE_TERMS)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


async def expand_and_collect(page) -> list[dict]:
    """Collect anchors, clicking 'show more' / 'next' up to MAX_PAGES times."""
    found: dict[str, dict] = {}
    for _ in range(MAX_PAGES):
        # lazy-loading lists: scroll to the bottom a few times
        for _ in range(3):
            await page.mouse.wheel(0, 20000)
            await page.wait_for_timeout(700)
        for a in await page.evaluate(EXTRACT_JS):
            found.setdefault(a["href"], a)
        # find a visible "show more" / "next page" control and click it
        has_more = await page.evaluate(MARK_MORE_JS, SHOW_MORE_RE.pattern)
        if not has_more:
            break
        before = len(found)
        try:
            await page.locator("[data-ukpm-more]").first.click(timeout=5000)
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)
        for a in await page.evaluate(EXTRACT_JS):
            found.setdefault(a["href"], a)
        if len(found) == before:
            break
    return list(found.values())


async def open_page(ctx, url):
    page = await ctx.new_page()
    await page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    await page.wait_for_timeout(2000)
    # accept-only-necessary cookie banners if they block the page (best effort)
    for txt in ["Reject all", "Reject All", "Only necessary", "Necessary only", "Accept necessary", "Decline"]:
        try:
            b = page.get_by_role("button", name=txt)
            if await b.count() and await b.first.is_visible():
                await b.first.click(timeout=2000)
                break
        except Exception:
            pass
    return page


async def scan_university(ctx, uni: dict, sem) -> dict:
    url = uni["vacancies_url"] or uni["landing_url"]
    res = {"name": uni["name"], "url": url, "status": "ok", "links": 0, "research": 0,
           "followed": "", "error": "", "jobs": []}
    if not url:
        res["status"] = "no_url"
        return res
    async with sem:
        page = None
        try:
            page = await open_page(ctx, url)
            anchors = await expand_and_collect(page)
            jobs = [a for a in anchors if is_research(a["title"], a["ctx"])]
            # landing page with no research links: follow one "vacancies" link
            if not jobs:
                cand = [a for a in anchors if VACANCY_LINK_RE.search(a["title"] or "") and norm_url(a["href"]) != norm_url(url)]
                if cand:
                    nxt = cand[0]["href"]
                    await page.close()
                    page = await open_page(ctx, nxt)
                    anchors = await expand_and_collect(page)
                    jobs = [a for a in anchors if is_research(a["title"], a["ctx"])]
                    res["followed"] = nxt
            res["links"] = len(anchors)
            # de-duplicate by normalised url
            uniq = {}
            for j in jobs:
                uniq.setdefault(norm_url(j["href"]), j)
            res["jobs"] = list(uniq.values())
            res["research"] = len(res["jobs"])
            if res["links"] < 5:
                res["status"] = "few_links"
        except Exception as e:
            res["status"] = "error"
            res["error"] = f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
        finally:
            if page:
                await page.close()
    print(f"[{res['status']:9}] {res['research']:3} research / {res['links']:4} links  {uni['name']}", flush=True)
    return res


async def fetch_text(ctx, url, sem) -> str:
    async with sem:
        page = None
        try:
            page = await open_page(ctx, url)
            return (await page.evaluate(TEXT_JS))[:MAX_TEXT]
        except Exception as e:
            return f"[could not open advert: {type(e).__name__}]"
        finally:
            if page:
                await page.close()


async def main():
    DATA.mkdir(exist_ok=True)
    (DATA / "new").mkdir(exist_ok=True)
    REPORTS.mkdir(exist_ok=True)
    unis = list(csv.DictReader(open(UNIS_CSV, newline="", encoding="utf-8")))
    if ONLY:
        unis = [u for u in unis if ONLY.lower() in u["name"].lower()]
    seen = load_json(DATA / "seen.json", {})
    first_run = not seen

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(user_agent=UA, locale="en-GB", viewport={"width": 1366, "height": 900})
        sem = asyncio.Semaphore(CONCURRENCY)
        results = await asyncio.gather(*(scan_university(ctx, u, sem) for u in unis))

        # what is new today?
        today_keys, new_items = set(), []
        for r in results:
            for j in r["jobs"]:
                k = key_for(r["name"], j["href"], j["title"])
                today_keys.add(k)
                if k in seen:
                    seen[k]["last_seen"] = TODAY
                    seen[k]["missing_days"] = 0
                else:
                    seen[k] = {"university": r["name"], "title": j["title"], "url": j["href"],
                               "first_seen": TODAY, "last_seen": TODAY, "missing_days": 0}
                    new_items.append({"key": k, "university": r["name"], "title": j["title"],
                                      "url": j["href"], "listing_context": j["ctx"]})
        for k, v in seen.items():
            if k not in today_keys:
                v["missing_days"] = v.get("missing_days", 0) + 1

        # full advert text for each new posting
        texts = await asyncio.gather(*(fetch_text(ctx, it["url"], sem) for it in new_items))
        for it, t in zip(new_items, texts):
            it["advert_text"] = t
            it["profile_score"] = score(it["title"] + " " + t)
        await browser.close()

    new_items.sort(key=lambda x: -x["profile_score"])
    payload = {"date": TODAY, "baseline_run": first_run, "count": len(new_items), "postings": new_items}
    (DATA / "new" / f"{TODAY}.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    (DATA / "latest_new.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    # forget postings that disappeared from the sites more than 60 days ago
    seen = {k: v for k, v in seen.items() if v.get("missing_days", 0) <= 60}
    (DATA / "seen.json").write_text(json.dumps(seen, indent=1, ensure_ascii=False))

    with open(DATA / "open_research_jobs.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["university", "title", "url", "first_seen", "last_seen"])
        for v in sorted(seen.values(), key=lambda v: (v["university"], v["title"])):
            if v.get("missing_days", 0) == 0:
                w.writerow([v["university"], v["title"], v["url"], v["first_seen"], v["last_seen"]])

    with open(DATA / "coverage.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["university", "status", "research_links", "all_links", "url", "followed", "error"])
        for r in results:
            w.writerow([r["name"], r["status"], r["research"], r["links"], r["url"], r["followed"], r["error"]])

    ok = [r for r in results if r["status"] == "ok"]
    zero = [r for r in ok if r["research"] == 0]
    bad = [r for r in results if r["status"] != "ok"]
    lines = [f"# Run {TODAY}", "",
             f"- Universities scanned: {len(results)}",
             f"- Pages loaded fine: {len(ok)} ({len(ok) - len(zero)} with research posts, {len(zero)} with none listed today)",
             f"- Problems: {len(bad)}",
             f"- Research postings listed today: {len(today_keys)}",
             f"- **New since last run: {len(new_items)}**" + (" (first run: everything counts as new)" if first_run else ""),
             "", "## New postings", ""]
    for it in new_items:
        lines.append(f"- [{it['title']}]({it['url']}) — {it['university']} (score {it['profile_score']})")
    lines += ["", "## Problems", ""]
    for r in bad:
        lines.append(f"- {r['name']}: {r['status']} {r['error']} <{r['url']}>")
    lines += ["", "## Loaded but no research posts found (check if this persists)", ""]
    for r in zero:
        lines.append(f"- {r['name']} ({r['links']} links) <{r['url']}>")
    (REPORTS / f"{TODAY}.md").write_text("\n".join(lines) + "\n")
    (REPORTS / "latest.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:8]))


if __name__ == "__main__":
    asyncio.run(main())
