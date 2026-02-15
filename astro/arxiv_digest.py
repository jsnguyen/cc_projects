#!/usr/bin/env python3
"""
arXiv astro-ph daily digest — scrapes today's new submissions and filters
by topics listed in topics.txt.
"""

import argparse
import json
import re
import sys
import textwrap
from pathlib import Path
from urllib.request import urlopen, Request

# ── Configuration ────────────────────────────────────────────────────────────
LIST_URL = "https://arxiv.org/list/astro-ph/new"
TOP_N = 10          # max papers to display (0 or None → show all matches)
TOPICS_FILE = Path(__file__).parent / "topics.txt"
# ─────────────────────────────────────────────────────────────────────────────


def load_topics(path: Path) -> list[str]:
    """Read topics from file, one per line. Ignores blanks and # comments."""
    if not path.exists():
        print(f"Warning: {path} not found. No topics to match.", file=sys.stderr)
        return []
    topics = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            topics.append(line)
    return topics


def fetch_page(url: str) -> str:
    """Download the listing page."""
    req = Request(url, headers={"User-Agent": "arxiv-digest/1.0"})
    with urlopen(req, timeout=30) as resp:
        return resp.read().decode()


def parse_listing(html: str) -> list[dict]:
    """Parse the arxiv /list/astro-ph/new HTML into a list of paper dicts.

    Only extracts papers from the "New submissions" section (stops at
    "Cross submissions").
    """
    # Isolate the "New submissions" section — everything between the first
    # <dl id='articles'> and the next <h3> containing "Cross submissions"
    new_start = html.find("<dl id='articles'>")
    if new_start == -1:
        new_start = html.find('<dl id="articles">')
    if new_start == -1:
        return []

    cross_match = re.search(r"<h3>Cross submissions", html[new_start:])
    if cross_match:
        section = html[new_start : new_start + cross_match.start()]
    else:
        section = html[new_start:]

    # Split into <dt>…</dt><dd>…</dd> pairs
    dt_parts = re.split(r"<dt>", section)[1:]  # skip text before first <dt>

    papers = []
    for dt_chunk in dt_parts:
        # Each chunk is: "...<dt content>...</dt><dd>...</dd>..."
        # Extract arXiv ID from the <dt> block
        id_match = re.search(r'href\s*=\s*"/abs/([^"]+)"', dt_chunk)
        if not id_match:
            continue
        arxiv_id = id_match.group(1)

        # Find the <dd> block
        dd_match = re.search(r"<dd>(.*)", dt_chunk, re.DOTALL)
        if not dd_match:
            continue
        dd = dd_match.group(1)

        # Title: inside <div class='list-title …'>
        title_match = re.search(
            r"<div class=['\"]list-title[^>]*>.*?<span class=['\"]descriptor['\"]>Title:</span>\s*(.*?)</div>",
            dd, re.DOTALL,
        )
        title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip() if title_match else ""

        # Authors: inside <div class='list-authors'>
        authors_match = re.search(
            r"<div class=['\"]list-authors['\"]>(.*?)</div>", dd, re.DOTALL
        )
        authors = []
        if authors_match:
            # Pull text from each <a> tag
            authors = [
                re.sub(r"<[^>]+>", "", a).strip()
                for a in re.findall(r"<a[^>]*>(.*?)</a>", authors_match.group(1))
            ]

        # Abstract: inside <p class='mathjax'>
        abs_match = re.search(
            r"<p class=['\"]mathjax['\"]>(.*?)</p>", dd, re.DOTALL
        )
        abstract = ""
        if abs_match:
            abstract = re.sub(r"<[^>]+>", "", abs_match.group(1)).strip()
            abstract = re.sub(r"\s+", " ", abstract)

        papers.append({
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "link": f"https://arxiv.org/abs/{arxiv_id}",
        })

    return papers


def match_topics(paper: dict, topics: list[str]) -> list[str]:
    """Return list of topics that match in the title or abstract."""
    text = (paper["title"] + " " + paper["abstract"]).lower()
    matched = []
    for topic in topics:
        if topic.lower() in text:
            matched.append(topic)
    return matched


def format_paper(idx: int, paper: dict, matched: list[str]) -> str:
    """Pretty-print a single paper entry (verbose mode)."""
    lines = []
    lines.append("=" * 80)
    lines.append(f"[{idx}] {paper['title']}")

    # First 3 authors
    authors = paper["authors"]
    if len(authors) > 3:
        author_str = ", ".join(authors[:3]) + f" + {len(authors) - 3} more"
    else:
        author_str = ", ".join(authors) if authors else "N/A"
    lines.append(f"    Authors: {author_str}")

    lines.append(f"    Matched topics: {', '.join(matched)}")
    lines.append(f"    arXiv: {paper['link']}")
    lines.append("")

    # Wrap abstract
    wrapped = textwrap.fill(paper["abstract"], width=76, initial_indent="    ", subsequent_indent="    ")
    lines.append("    Abstract:")
    lines.append(wrapped)
    lines.append("=" * 80)
    return "\n".join(lines)


def format_paper_short(paper: dict, matched: list[str]) -> str:
    """Compact 3-line format: linked title, authors, matched topics."""
    # Title with embedded markdown link
    title_line = f"[{paper['title']}]({paper['link']})"

    # Up to 3 authors
    authors = paper["authors"]
    if len(authors) > 3:
        author_line = ", ".join(authors[:3]) + f" + {len(authors) - 3} more"
    else:
        author_line = ", ".join(authors) if authors else "N/A"

    topics_line = ", ".join(matched)

    return f"{title_line}\n{author_line}\n{topics_line}"


def main():
    parser = argparse.ArgumentParser(description="arXiv astro-ph daily digest")
    parser.add_argument(
        "--short", action="store_true",
        help="Compact 3-line output per paper (title with link, authors, topics)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output as JSON",
    )
    args = parser.parse_args()

    topics = load_topics(TOPICS_FILE)
    if not topics:
        print("No topics loaded. Add topics to topics.txt (one per line).")
        sys.exit(1)

    if not args.short and not args.json:
        print(f"Topics: {', '.join(topics)}")
    if not args.json:
        print(f"Fetching {LIST_URL} ...")

    html = fetch_page(LIST_URL)
    papers = parse_listing(html)

    if not args.short and not args.json:
        print(f"Parsed {len(papers)} new submissions.\n")

    # Score and filter
    results = []
    for paper in papers:
        matched = match_topics(paper, topics)
        if matched:
            results.append((paper, matched))

    # Sort by number of matched topics (most relevant first)
    results.sort(key=lambda x: len(x[1]), reverse=True)

    total_matches = len(results)
    if TOP_N and TOP_N > 0:
        results = results[:TOP_N]

    if args.json:
        output = {
            "source": LIST_URL,
            "topics": topics,
            "total_new_submissions": len(papers),
            "total_matches": total_matches,
            "num_returned": len(results),
            "papers": [
                {**paper, "matched_topics": matched}
                for paper, matched in results
            ],
        }
        print(json.dumps(output, indent=2))
        return

    if not results:
        print("No papers matched your topics today.")
        return

    if args.short:
        for paper, matched in results:
            print(format_paper_short(paper, matched))
            print()
    else:
        for i, (paper, matched) in enumerate(results, 1):
            print(format_paper(i, paper, matched))
            print()

    # Summary
    if TOP_N and TOP_N > 0 and total_matches > TOP_N:
        print(f"Showing top {TOP_N} of {total_matches} matches (out of {len(papers)} new submissions)")
    else:
        print(f"Found {total_matches} matching papers (out of {len(papers)} new submissions)")


if __name__ == "__main__":
    main()
