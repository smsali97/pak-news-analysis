# pak-news-analysis

International / world news collection for analysis, across Pakistani outlets and
The Guardian. Each collector writes the **same CSV schema** so outputs merge into
one corpus.

## Schema

`outlet, section, date, timestamp, author, headline, url, article_id, body`

- `timestamp` is the full ISO-8601 publish time; `date` is `YYYY-MM-DD`.
- `section` is the source's section id (e.g. `world`); rows are world/international only.
- `body` is the full article text.

## Collectors

| Script | Source | Method |
|---|---|---|
| `collect_archive.py` | Dawn (date-aware archive), Tribune (sitemaps) | scrape + section meta |
| `collect_guardian.py` | The Guardian | Content API (full body in one call) |
| `run_collect.py` | orchestrates `collect_archive.py` by month, merges | subprocess per month |
| `scrape_articles.py` | Dawn/Tribune/The News search | quick ad-hoc scrape |

All collectors checkpoint and resume losslessly: kill any run, rerun the same
command, and it continues. Output is de-duplicated and timestamp-sorted.

## Data

- `dawn_intl.csv` — Dawn world news, 2025-10-06 → 2026-06-15 (~3.6k articles).
- `data/intl_YYYY_MM.csv` — per-month chunks that merge into `dawn_intl.csv`.
- `guardian_news.csv` — Guardian US/Iran/Pakistan coverage, 2025-10 → 2026-06
  (~1.6k articles). Built by unioning topic tags (`world/iran`,
  `world/us-israel-war-on-iran`, `world/pakistan`) filtered to `pillar/news`,
  deduped by Guardian `article_id`. Tags (not section) keep misses low: it
  catches on-topic news wherever it is filed (e.g. `us-news`, `politics`).
- `articles.csv` — earlier ad-hoc scrape.

## Usage

```bash
pip install requests beautifulsoup4 lxml

# Dawn world news, full window, month-by-month, resumable:
python3 run_collect.py --start 2025-10-06 --outlets dawn --merged dawn_intl.csv

# Guardian (US/Iran/Pakistan relations by default; configurable via --q / --section):
export GUARDIAN_API_KEY=...          # or add fallback keys to guardian_keys.txt
python3 collect_guardian.py
```

### API keys (Guardian)

Provide one or more keys; the collector rotates to the next on rejection or daily
quota. In priority order: `--api-key` (comma-separated), `GUARDIAN_API_KEY` /
`GUARDIAN_API_KEYS`, then `--keys-file` / `guardian_keys.txt` (one per line).
Key files and `.env` are gitignored — never commit keys.
