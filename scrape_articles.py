#!/usr/bin/env python3
"""
scrape_articles.py — collect full article text from Pakistani news outlets.

STAGE 1 of the pipeline. Output feeds extract_sources.py.

Lesson learned the hard way: these sites use bot detection. The three things
that matter are (1) a real browser User-Agent, (2) a Referer header, and
(3) a polite delay between requests. With those, article pages load fine.
Without them you get a 403. This script does all three.

INPUT  : a text file of article URLs, one per line (urls.txt), OR a search
         crawl if you pass --search (all three outlets, paginated).
OUTPUT : articles.csv with columns:
         article_id, outlet, url, date, author, genre, headline, body

USAGE:
    # from a list of URLs you gathered
    python3 scrape_articles.py --urls urls.txt --out articles.csv

    # crawl every outlet's search for a query, up to 40 links each
    python3 scrape_articles.py --search "out of school children" --max 40

    # restrict the crawl to one outlet
    python3 scrape_articles.py --search "out of school children" --site dawn.com

NOTE ON GENRE:
    The script guesses genre from the URL/section: /opinion or /newspaper
    columns -> 'editorial/opinion'; authors 'AFP'/'Reuters'/'APP' -> 'wire';
    everything else -> 'news'. Genre matters because, as the pilot showed,
    wire features quote teachers while domestic policy news quotes ministers.
"""

import argparse
import csv
import re
import sys
import time
import random
from urllib.parse import urljoin, quote_plus, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Run:  pip install requests beautifulsoup4 lxml")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
    "Connection": "keep-alive",
}

OUTLET_RULES = {
    "dawn.com": {
        "body_sel": "div.story__content p, article p",
        "title_sel": "h1, h2.story__title",
        "author_sel": "a[href*='/authors/']",
        "search_url": "https://www.dawn.com/search?query={q}",
        "link_sel": "a[href*='/news/']",
        "page_param": "page",
    },
    "tribune.com.pk": {
        "body_sel": "span.story-text p, div.story-text p, div.tp-content p",
        "title_sel": "h1.title, h1",
        "author_sel": "a.author, span.author",
        "search_url": "https://tribune.com.pk/search?q={q}",
        "link_sel": "a[href*='/story/']",
        "page_param": "page",
    },
    "thenews.com.pk": {
        "body_sel": "div.story-detail p, div.detail-content p",
        "title_sel": "h1.title, h1",
        "author_sel": "span.category-source, span.author",
        "search_url": "https://www.thenews.com.pk/search?q={q}",
        "link_sel": "a[href*='/print/'], a[href*='/tns/']",
        "page_param": "page",
    },
}


def outlet_of(url: str) -> str:
    for dom in OUTLET_RULES:
        if dom in url:
            return dom
    return ""


def fetch(url: str) -> BeautifulSoup | None:
    try:
        time.sleep(random.uniform(1.5, 3.5))  # human-ish pacing
        r = requests.get(url, headers=HEADERS, timeout=25)
        if r.status_code != 200:
            print(f"    {r.status_code} on {url[:60]}")
            return None
        return BeautifulSoup(r.text, "lxml")
    except Exception as e:
        print(f"    error {url[:60]}: {e}")
        return None


def guess_genre(url: str, author: str) -> str:
    a = (author or "").lower()
    if any(w in a for w in ("afp", "reuters", "app", "ap ", "anadolu")):
        return "wire"
    if "/opinion" in url or "/newspaper/column" in url or "editorial" in url:
        return "editorial/opinion"
    return "news"


def scrape_one(url: str, idx: int) -> dict | None:
    dom = outlet_of(url)
    if not dom:
        print(f"    unknown outlet: {url[:60]}")
        return None
    rules = OUTLET_RULES[dom]
    soup = fetch(url)
    if not soup:
        return None

    title_el = soup.select_one(rules["title_sel"])
    title = title_el.get_text(strip=True) if title_el else ""

    author_el = soup.select_one(rules["author_sel"])
    author = author_el.get_text(strip=True) if author_el else ""

    paras = [p.get_text(" ", strip=True) for p in soup.select(rules["body_sel"])]
    body = " ".join(p for p in paras if len(p) > 30)
    if len(body) < 120:
        print(f"    body too short, skipping {url[:60]}")
        return None

    # date from a <time> tag or meta
    date = ""
    t = soup.find("time")
    if t and t.get("datetime"):
        date = t["datetime"][:10]
    else:
        m = soup.find("meta", {"property": "article:published_time"})
        if m and m.get("content"):
            date = m["content"][:10]

    return {
        "article_id": re.sub(r"\D", "", url)[-7:] or f"art{idx}",
        "outlet": dom.split(".")[0],
        "url": url,
        "date": date,
        "author": author,
        "genre": guess_genre(url, author),
        "headline": title,
        "body": body,
    }


def crawl_search(dom: str, query: str, max_n: int) -> list:
    rules = OUTLET_RULES[dom]
    page_param = rules.get("page_param")
    links = []
    page = 1
    while len(links) < max_n:
        url = rules["search_url"].format(q=quote_plus(query))
        if page > 1:
            if not page_param:
                break
            url += f"&{page_param}={page}"
        soup = fetch(url)
        if not soup:
            break
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        before = len(links)
        for a in soup.select(rules["link_sel"]):
            href = urljoin(base, a.get("href", "")).split("#")[0]
            if dom in href and href not in links:
                links.append(href)
            if len(links) >= max_n:
                break
        if len(links) == before:  # page yielded nothing new -> stop paging
            break
        page += 1
    return links


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--urls", help="text file of article URLs, one per line")
    p.add_argument("--search", help="crawl each outlet's search for this query")
    p.add_argument("--site", choices=list(OUTLET_RULES),
                   help="restrict --search to one outlet (default: all)")
    p.add_argument("--max", type=int, default=40,
                   help="max links to crawl per outlet (default 40)")
    p.add_argument("--out", default="articles.csv")
    args = p.parse_args()

    urls = []
    if args.urls:
        with open(args.urls) as f:
            urls = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    if args.search:
        targets = [args.site] if args.site else list(OUTLET_RULES)
        for dom in targets:
            print(f"Crawling {dom}: {args.search}")
            found = crawl_search(dom, args.search, args.max)
            print(f"  {len(found)} links")
            urls += found
    if not urls:
        sys.exit("No URLs. Pass --urls urls.txt or --search 'query'.")

    seen = set()
    urls = [u for u in urls if not (u in seen or seen.add(u))]

    rows = []
    for i, u in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {u[:70]}")
        rec = scrape_one(u, i)
        if rec:
            rows.append(rec)

    if not rows:
        sys.exit("Nothing scraped.")
    cols = ["article_id", "outlet", "url", "date", "author", "genre", "headline", "body"]
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"\nScraped {len(rows)} articles -> {args.out}")
    print(f"Next:  python3 extract_sources.py {args.out}")


if __name__ == "__main__":
    main()
