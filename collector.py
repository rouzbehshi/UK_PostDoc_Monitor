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

CONCURRENCY = int(os.environ.get("CONCURRENCY", "5"))
PAGE_TIMEOUT_MS = 45_000
MAX_PAGES = 6            # pagination / "show more" clicks per site
SITE_TIMEOUT_S = 240     # give up on one site after this long
BACKLOG_MIN_SCORE = 3    # first run only: minimum topic score for a post to be handed on
MAX_TEXT = 20_000        # characters of advert text kept per posting
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
ONLY = os.environ.get("ONLY")
BROWSER = None  # optional: substring filter on university name, for testing

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
            await page.wait_for_load_state("networkidle", timeout=8000)
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
        await page.wait_for_load_state("networkidle", timeout=10000)
    except PWTimeout:
        pass
    await page.wait_for_timeout(1200)
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


BLOCK_RE = re.compile(r"(performing security verification|verify you are human|just a moment|max challenge attempts|access denied|attention required)", re.I)
FOLLOW_RULES = [  # (score, regex on link text + href)
    (10, re.compile(r"(cat=-1|full\s+list|view[\s_-]*all|viewalljobs|all\s+(current\s+)?(jobs|vacanc|opportunit|roles)|/requisitions|vacancy-search-results|search-results)", re.I)),
    (7, re.compile(r"research.{0,40}(careers|jobs|opportunit|teaching|\(\d+\))|(academic|education).{0,25}research|research\s*(and|&|/)\s*(teaching|enterprise|clinical)", re.I)),
    (4, re.compile(r"(current\s+(vacanc|jobs|opportunit)|search\s+(and\s+apply|our\s+jobs|jobs|vacanc|for\s+jobs)|job\s+search|find\s+(a\s+)?job|browse\s+jobs|latest\s+vacanc|external\s+vacanc|^vacancies$|^jobs$)", re.I)),
]
FOLLOW_SKIP = re.compile(r"(login|logon|log-in|sign[\s-]?in|register|registration|faq|cookie|privacy|terms|accessib|rss|alert|subs/|internal|mailto:|\.pdf|facebook|twitter|linkedin|instagram|youtube|studentship|phd|apprentice|casual|unitemps)", re.I)
SEARCH_BTN_RE = re.compile(r"^(search|search jobs|search vacancies|find jobs|search for jobs|show jobs|view jobs|go)$", re.I)


def research_jobs(anchors):
    uniq = {}
    for a in anchors:
        if is_research(a["title"], a["ctx"]):
            uniq.setdefault(norm_url(a["href"]), a)
    return list(uniq.values())


def same_site(u1, u2):
    h1, h2 = urlparse(u1).netloc.lower(), urlparse(u2).netloc.lower()
    base = lambda h: ".".join(h.split(".")[-3:]) if h.endswith(".ac.uk") else ".".join(h.split(".")[-2:])
    return base(h1) == base(h2)


def follow_candidates(anchors, start_url, uni_urls):
    scored = {}
    for a in anchors:
        h = a["href"]
        blob = f"{a['title']} {h}"
        if FOLLOW_SKIP.search(blob) or norm_url(h) == norm_url(start_url):
            continue
        if not any(same_site(h, u) for u in uni_urls) and not re.search(
                r"(webitrent|taleo|oraclecloud|corehr|engageats|myworkdayjobs|pinpointhq|teamtailor|successfactors|tal\.net|eploy|jobtrain|//jobs\.|//vacancies\.|//careers\.|//recruitment\.)", h, re.I):
            continue
        for sc, rx in FOLLOW_RULES:
            if rx.search(blob):
                scored[norm_url(h)] = max(scored.get(norm_url(h), 0), sc)
                break
    return [u for u, _ in sorted(scored.items(), key=lambda x: -x[1])]


async def try_search_button(page):
    """Click an empty 'Search' / 'Find jobs' button (CoreHR, webitrent, engage...) and return anchors."""
    try:
        clicked = await page.evaluate(r"""(pat) => {
          const re = new RegExp(pat, 'i');
          for (const el of document.querySelectorAll('button, input[type=submit], input[type=button], a[role=button]')) {
            const l = (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g,' ').trim();
            const r = el.getBoundingClientRect();
            if (re.test(l) && r.width > 0 && r.height > 0) { el.setAttribute('data-ukpm-search','1'); return true; }
          }
          return false; }""", SEARCH_BTN_RE.pattern)
        if not clicked:
            return []
        await page.locator("[data-ukpm-search]").first.click(timeout=5000)
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except PWTimeout:
            pass
        await page.wait_for_timeout(2500)
        return await expand_and_collect(page)
    except Exception:
        return []


async def oracle_jobs(ctx, url):
    """Oracle Recruiting Cloud: read the public job-search REST API directly."""
    m = re.search(r"^(https://[^/]+)/hcmUI/CandidateExperience/[a-z]{2}/sites/([A-Za-z0-9_]+)", url)
    if not m:
        return None
    base, site = m.group(1), m.group(2)
    out, offset = [], 0
    while offset < 1000:
        api = (f"{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList"
               f"&finder=findReqs;siteNumber={site},limit=100,offset={offset},sortBy=POSTING_DATES_DESC")
        r = await ctx.request.get(api, timeout=30000)
        if r.status != 200:
            break
        items = (await r.json()).get("items") or [{}]
        reqs = items[0].get("requisitionList") or []
        for q in reqs:
            out.append({"href": f"{base}/hcmUI/CandidateExperience/en/sites/{site}/job/{q.get('Id')}",
                        "title": (q.get("Title") or "").strip(), "ctx": f"{q.get('PrimaryLocation') or ''} | posted {q.get('PostedDate') or ''}",
                        "oracle": {"base": base, "site": site, "id": q.get("Id")}})
        total = items[0].get("TotalJobsCount") or 0
        offset += 100
        if not reqs or offset >= total:
            break
    return out


async def oracle_text(ctx, o):
    api = (f"{o['base']}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
           f"&finder=ById;Id=%22{o['id']}%22,siteNumber={o['site']}")
    r = await ctx.request.get(api, timeout=30000)
    it = ((await r.json()).get("items") or [{}])[0]
    parts = [it.get("Title") or "", it.get("PrimaryLocation") or "", it.get("ExternalDescriptionStr") or "",
             it.get("ExternalResponsibilitiesStr") or "", it.get("ExternalQualificationsStr") or ""]
    html = "\n".join(p for p in parts if p)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"<[^>]+>", "\n", html)).strip()


async def scan_page_tree(ctx, start_url, uni_urls, res):
    """Open start_url; if it has no research links, explore up to 4 promising links (view all,
    research category, current vacancies...) and, failing that, press an empty Search button."""
    page = await open_page(ctx, start_url)
    try:
        body = (await page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 3000)")) or ""
        if BLOCK_RE.search(body) or BLOCK_RE.search(await page.title()):
            res["status"] = "blocked"
            return []
        if "/hcmUI/CandidateExperience/" in page.url:
            oj = await oracle_jobs(ctx, page.url)
            if oj is not None:
                res["followed"] = "oracle-api"
                res["links"] = len(oj)
                return research_jobs(oj) if oj else []
        anchors = await expand_and_collect(page)
        res["links"] = len(anchors)
        jobs = research_jobs(anchors)
        if jobs:
            return jobs
        visited = []
        for nxt in follow_candidates(anchors, page.url, uni_urls)[:3]:
            p2 = await open_page(ctx, nxt)
            try:
                if "/hcmUI/CandidateExperience/" in p2.url:
                    a2 = await oracle_jobs(ctx, p2.url) or []
                else:
                    a2 = await expand_and_collect(p2)
                visited.append(nxt)
                res["links"] += len(a2)
                jobs += research_jobs(a2)
                if not jobs and len(visited) == 1:
                    a3 = await try_search_button(p2)
                    jobs += research_jobs(a3)
            finally:
                await p2.close()
        res["followed"] = " ; ".join(visited)
        if not jobs:
            a4 = await try_search_button(page)
            if a4:
                res["followed"] += " ; [search button]"
                res["links"] += len(a4)
                jobs += research_jobs(a4)
        return jobs
    finally:
        await page.close()


async def scan_university(ctx, uni: dict, sem) -> dict:
    urls = [u for u in (uni["vacancies_url"], uni["landing_url"]) if u]
    res = {"name": uni["name"], "url": urls[0] if urls else "", "status": "ok", "links": 0, "research": 0,
           "followed": "", "error": "", "jobs": []}
    if not urls:
        res["status"] = "no_url"
        return res
    async with sem:
        site_ctx = await BROWSER.new_context(user_agent=UA, locale="en-GB", viewport={"width": 1366, "height": 900})
        site_ctx.set_default_timeout(20000)
        try:
            for i, url in enumerate(urls):
                res.update(status="ok", error="", url=url)
                try:
                    jobs = await asyncio.wait_for(scan_page_tree(site_ctx, url, urls, res), timeout=SITE_TIMEOUT_S)
                    uniq = {}
                    for j in jobs:
                        uniq.setdefault(norm_url(j["href"]), j)
                    res["jobs"] = list(uniq.values())
                    res["research"] = len(res["jobs"])
                    if res["status"] == "ok" and res["links"] < 5:
                        res["status"] = "few_links"
                except BaseException as e:  # includes timeouts/cancellations of this one site
                    if isinstance(e, KeyboardInterrupt):
                        raise
                    res["status"] = "error"
                    res["error"] = f"{type(e).__name__}: {(str(e).splitlines() or [''])[0][:200]}"
                if res["research"] or (res["status"] == "ok" and i == 0 and res["links"] >= 15):
                    break
        finally:
            try:
                await asyncio.wait_for(site_ctx.close(), timeout=20)
            except BaseException:
                pass
    print(f"[{res['status']:9}] {res['research']:3} research / {res['links']:4} links  {uni['name']}", flush=True)
    return res


# ---------------------------------------------------------------- jobs.ac.uk, one employer page per institution
JOBSAC_OVERRIDES = {
    "University College London": ["ucl"],
    "University of Manchester": ["the-university-of-manchester"],
    "London School of Economics and Political Science": ["london-school-of-economics-and-political-science", "the-london-school-of-economics-and-political-science-lse", "lse"],
    "Imperial College London": ["imperial-college-london"],
    "King's College London": ["kings-college-london"],
    "Queen's University Belfast": ["queens-university-belfast"],
    "City St George's, University of London": ["city-st-georges-university-of-london"],
    "St Mary's University, Twickenham": ["st-marys-university-twickenham"],
    "Coventry University": ["coventry-university-group", "coventry-university"],
    "Open University": ["the-open-university"],
    "University of Edinburgh": ["the-university-of-edinburgh", "university-of-edinburgh"],
    "University of Sheffield": ["the-university-of-sheffield", "university-of-sheffield"],
    "University of Nottingham": ["the-university-of-nottingham", "university-of-nottingham"],
    "University of Warwick": ["the-university-of-warwick", "university-of-warwick"],
    "University of the West of England": ["university-of-the-west-of-england-bristol-uwe", "uwe-bristol", "university-of-the-west-of-england"],
    "Institute of Cancer Research": ["the-institute-of-cancer-research"],
    "London School of Hygiene & Tropical Medicine": ["london-school-of-hygiene-tropical-medicine", "london-school-of-hygiene-and-tropical-medicine"],
}


def slugify(name):
    s = name.lower().replace("&", "and").replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>|</tr>", "\n", html)
    txt = re.sub(r"<[^>]+>", " ", html)
    txt = (txt.replace("&amp;", "&").replace("&nbsp;", " ").replace("&#39;", "'").replace("&quot;", '"')
              .replace("&lt;", "<").replace("&gt;", ">").replace("&pound;", "£"))
    txt = re.sub(r"[ \t]+", " ", txt)
    return re.sub(r"\n\s*\n+", "\n\n", txt).strip()


JOBSAC_A = re.compile(r'<a[^>]+href="((?:https://www\.jobs\.ac\.uk)?/job/[A-Z0-9]{5,8}[^"]*)"[^>]*>(.*?)</a>', re.S | re.I)


async def jobsac_scan(ctx, uni, sem, slug_cache):
    """Read the institution's jobs.ac.uk employer page(s) with plain HTTP (no browser)."""
    name = uni["name"]
    res = {"name": name, "slug": "", "status": "ok", "jobs": [], "pages": 0}
    cands = [slug_cache[name]] if slug_cache.get(name) else (
        JOBSAC_OVERRIDES.get(name, []) + [slugify(name), "the-" + slugify(name)])
    async with sem:
        for slug in dict.fromkeys(cands):
            found, use_browser = {}, False
            try:
                for k in range(15):
                    url = f"https://www.jobs.ac.uk/search/employer/{slug}?sortOrder=1&pageSize=25&startIndex={1 + 25 * k}"
                    r = await ctx.request.get(url, timeout=30000)
                    html = await r.text()
                    if use_browser or (r.status == 200 and not JOBSAC_A.search(html)):
                        use_browser = True  # results rendered by script: fall back to the browser
                        pg = await open_page(ctx, url)
                        try:
                            html = await pg.content()
                        finally:
                            await pg.close()
                    m = re.search(r"<title>\s*(.*?)\s*</title>", html, re.S)
                    title = m.group(1) if m else ""
                    if k == 0 and (r.status != 200 or " Jobs - Apply Online at jobs.ac.uk" not in title):
                        break
                    employer = title.split(" Jobs - Apply Online")[0].strip()
                    new = 0
                    for mm in JOBSAC_A.finditer(html):
                        href = urljoin("https://www.jobs.ac.uk", mm.group(1))
                        t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", mm.group(2))).strip()
                        after = html[mm.end(): mm.end() + 1500]
                        if employer and employer.lower() not in html_to_text(after).lower():
                            continue  # featured job from another employer
                        if norm_url(href) not in found and t:
                            found[norm_url(href)] = {"href": href, "title": t[:250], "ctx": html_to_text(after)[:300]}
                            new += 1
                    res["pages"] = k + 1
                    if new == 0:
                        break
            except Exception as e:
                res["status"] = f"error: {type(e).__name__}"
            if found:
                res["slug"] = slug
                slug_cache[name] = slug
                res["jobs"] = [a for a in found.values() if is_research(a["title"], a["ctx"])]
                res["all"] = len(found)
                break
        if not res["slug"] and res["status"] == "ok":
            res["status"] = "employer not found"
    print(f"[jobs.ac.uk] {len(res['jobs']):3} research / {res.get('all', 0):3} jobs  {name} ({res['slug'] or res['status']})", flush=True)
    return res


async def fetch_text(ctx, url, sem, oracle=None) -> str:
    async with sem:
        page = None
        try:
            if "jobs.ac.uk/job/" in url:
                r = await ctx.request.get(url, timeout=30000)
                t = html_to_text(await r.text())
                i = t.find("Back to search results")
                return (t[i + 22:] if 0 <= i < 3000 else t)[:MAX_TEXT]
            if oracle:
                try:
                    t = await oracle_text(ctx, oracle)
                    if len(t) > 200:
                        return t[:MAX_TEXT]
                except Exception:
                    pass
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
        global BROWSER
        browser = BROWSER = await p.chromium.launch(args=["--disable-dev-shm-usage"])
        ctx = await browser.new_context(user_agent=UA, locale="en-GB", viewport={"width": 1366, "height": 900})
        sem = asyncio.Semaphore(CONCURRENCY)
        results = await asyncio.gather(*(scan_university(ctx, u, sem) for u in unis))
        slug_cache = load_json(DATA / "jobsac_slugs.json", {})
        jsem = asyncio.Semaphore(3)  # be gentle with jobs.ac.uk
        jac = {r["name"]: r for r in await asyncio.gather(*(jobsac_scan(ctx, u, jsem, slug_cache) for u in unis))}
        (DATA / "jobsac_slugs.json").write_text(json.dumps(slug_cache, indent=1, sort_keys=True))

        # merge both sources; the same post on the uni site and on jobs.ac.uk counts once
        def tkey(uni, title):
            t = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
            return f"{uni}|{t}" if len(t) >= 25 else None
        seen_titles = {v["tkey"]: k for k, v in seen.items() if v.get("tkey")}
        today_keys, new_items, today_titles = set(), [], {}
        for r in results:
            for j in r["jobs"]:
                j["source"] = "university site"
            for j in jac.get(r["name"], {}).get("jobs", []):
                j["source"] = "jobs.ac.uk"
            for j in r["jobs"] + jac.get(r["name"], {}).get("jobs", []):
                tk = tkey(r["name"], j["title"])
                if tk and tk in today_titles:          # duplicate of a post already handled today
                    continue
                k = key_for(r["name"], j["href"], j["title"])
                if k not in seen and tk and tk in seen_titles:   # seen before under the other source
                    k = seen_titles[tk]
                if tk:
                    today_titles[tk] = k
                today_keys.add(k)
                if k in seen:
                    seen[k]["last_seen"] = TODAY
                    seen[k]["missing_days"] = 0
                else:
                    seen[k] = {"university": r["name"], "title": j["title"], "url": j["href"], "source": j["source"],
                               "tkey": tk, "first_seen": TODAY, "last_seen": TODAY, "missing_days": 0}
                    new_items.append({"key": k, "university": r["name"], "title": j["title"], "url": j["href"],
                                      "source": j["source"], "listing_context": j["ctx"], "_oracle": j.get("oracle")})
        for k, v in seen.items():
            if k not in today_keys:
                v["missing_days"] = v.get("missing_days", 0) + 1

        # full advert text for each new posting
        tsem = asyncio.Semaphore(8)
        texts = await asyncio.gather(*(fetch_text(ctx, it["url"], tsem, it.pop("_oracle")) for it in new_items))
        for it, t in zip(new_items, texts):
            it["advert_text"] = t
            it["profile_score"] = score(it["title"] + " " + t)
        await browser.close()

    new_items.sort(key=lambda x: -x["profile_score"])
    backlog_total = len(new_items)
    if first_run:
        # First run = every currently open post. Hand on only those whose advert mentions the
        # profile topics at least a few times; from tomorrow every new post is handed on in full.
        new_items = [it for it in new_items if it["profile_score"] >= BACKLOG_MIN_SCORE]
    payload = {"date": TODAY, "baseline_run": first_run, "count": len(new_items),
               "backlog_total": backlog_total if first_run else None, "postings": new_items}
    (DATA / "new" / f"{TODAY}.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    (DATA / "latest_new.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    # forget postings that disappeared from the sites more than 60 days ago
    seen = {k: v for k, v in seen.items() if v.get("missing_days", 0) <= 60}
    (DATA / "seen.json").write_text(json.dumps(seen, indent=1, ensure_ascii=False))

    with open(DATA / "open_research_jobs.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["university", "title", "url", "source", "first_seen", "last_seen"])
        for v in sorted(seen.values(), key=lambda v: (v["university"], v["title"])):
            if v.get("missing_days", 0) == 0:
                w.writerow([v["university"], v["title"], v["url"], v.get("source", ""), v["first_seen"], v["last_seen"]])

    with open(DATA / "coverage.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["university", "status", "research_links", "all_links", "jobsac_research", "jobsac_slug", "url", "followed", "error"])
        for r in results:
            ja = jac.get(r["name"], {})
            w.writerow([r["name"], r["status"], r["research"], r["links"], len(ja.get("jobs", [])), ja.get("slug") or ja.get("status", ""),
                        r["url"], r["followed"], r["error"]])

    ok = [r for r in results if r["status"] == "ok"]
    zero = [r for r in ok if r["research"] == 0]
    bad = [r for r in results if r["status"] != "ok"]
    lines = [f"# Run {TODAY}", "",
             f"- Universities scanned: {len(results)}",
             f"- Pages loaded fine: {len(ok)} ({len(ok) - len(zero)} with research posts, {len(zero)} with none listed today)",
             f"- Problems: {len(bad)}",
             f"- jobs.ac.uk employer pages found: {sum(1 for v in jac.values() if v.get('slug'))} of {len(jac)}",
             f"- Research postings listed today (both sources, de-duplicated): {len(today_keys)}",
             (f"- **First run:** {backlog_total} open research posts recorded; {len(new_items)} mention the profile topics and were handed on"
              if first_run else f"- **New since last run: {len(new_items)}**"),
             "", "## New postings", ""]
    for it in new_items:
        lines.append(f"- [{it['title']}]({it['url']}) — {it['university']} via {it['source']} (score {it['profile_score']})")
    lines += ["", "## Problems", ""]
    for r in bad:
        lines.append(f"- {r['name']}: {r['status']} {r['error']} <{r['url']}>")
    lines += ["", "## Loaded but no research posts found on the university site (check if this persists)", ""]
    for r in zero:
        lines.append(f"- {r['name']} ({r['links']} links; jobs.ac.uk: {len(jac.get(r['name'], {}).get('jobs', []))} research) <{r['url']}>")
    (REPORTS / f"{TODAY}.md").write_text("\n".join(lines) + "\n")
    (REPORTS / "latest.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:8]))


def _heartbeat():
    import threading, time
    def run():
        t0 = time.time()
        while True:
            time.sleep(60)
            try:
                mem = {l.split(":")[0]: l.split()[1] for l in open("/proc/meminfo") if l.startswith(("MemAvailable", "MemTotal"))}
            except Exception:
                mem = {}
            print(f"[heartbeat] {int(time.time() - t0)}s elapsed, memory available {int(mem.get('MemAvailable', 0)) // 1024} MB", flush=True)
    threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    import faulthandler, signal, traceback
    faulthandler.enable()
    faulthandler.register(signal.SIGTERM, all_threads=True)
    _heartbeat()
    try:
        asyncio.run(main())
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        raise
