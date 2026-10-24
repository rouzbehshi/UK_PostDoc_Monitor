# UK PostDoc Monitor

Checks the vacancies pages of ~160 UK universities and research institutions every morning
and records every **new research post** (Research Associate / Assistant / Fellow, Postdoc,
Researcher, ...), together with the full text of the advert.

Titles are filtered only by *type of post*, never by topic: a bare "Research Associate" is
kept and its advert is read in full later, because the title rarely says what the work is.

## Files

| File | What it is |
|---|---|
| `universities.csv` | Institutions and their vacancies pages. Edit this to add/fix a site. |
| `collector.py` | The collector (headless Chromium via Playwright). |
| `data/latest_new.json` | New postings from the latest run, with full advert text. |
| `data/new/YYYY-MM-DD.json` | Same, kept per day. |
| `data/open_research_jobs.csv` | All research postings currently listed on the sites. |
| `data/seen.json` | Everything seen so far (used to detect new postings). |
| `data/coverage.csv` | Per-site diagnostics: loaded or not, links found. |
| `reports/latest.md` | Readable summary of the latest run. |

## Running

Runs automatically every day at 04:30 UTC (GitHub Actions → *Collect university research jobs*).
To run now: **Actions** tab → *Collect university research jobs* → **Run workflow**.

Locally:

```bash
pip install -r requirements.txt && python -m playwright install chromium
python collector.py                 # all sites
ONLY="Bath" python collector.py     # one site, for testing
```
