#!/usr/bin/env python3
"""CLI tool to search NASA ADS / SciX papers via the API.

Requires ADS_API_TOKEN env var. Get one at:
https://ui.adsabs.harvard.edu/user/settings/token
"""

import argparse
import json
import sys
import textwrap

from scixhub import SciXClient, SciXError

FIELDS = (
    "bibcode,title,author,abstract,year,doi,citation_count,"
    "read_count,pub,pubdate,doctype,identifier,keyword"
)

SORT_MAP = {
    "citations": "citation_count desc",
    "date": "date desc",
    "reads": "read_count desc",
}


def build_query(args):
    """Construct ADS query string and kwargs for SciXClient.search()."""
    parts = []

    if args.bibcode:
        return f"bibcode:{args.bibcode}", {"rows": 1}

    if args.title:
        parts.append(f'title:"{args.title}"')
    if args.author:
        parts.append(f'author:"{args.author}"')
    if args.year:
        parts.append(f"year:{args.year}")
    if args.query:
        parts.append(args.query)

    if not parts:
        print("No search terms provided.")
        sys.exit(1)

    q = " ".join(parts)
    kwargs = {"rows": args.num}
    if args.sort:
        kwargs["sort"] = SORT_MAP.get(args.sort, args.sort)
    return q, kwargs


def _author_short(authors):
    """Format as 'LastName et al.' for terse display."""
    if not authors:
        return "Unknown"
    first = authors[0].split(",")[0]
    if len(authors) == 1:
        return first
    return f"{first} et al."


def _author_compact(authors):
    """Format as 'FirstAuthor+N' for brief display."""
    if not authors:
        return "Unknown"
    first = authors[0].split(",")[0]
    if len(authors) == 1:
        return first
    return f"{first}+{len(authors) - 1}"


def _truncate(text, length=200):
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0] + "..."


def _author_verbose(authors):
    """Format as 'Last, F.' for verbose display."""
    formatted = []
    for a in authors:
        parts = a.split(",", 1)
        if len(parts) == 2:
            last = parts[0].strip()
            first = parts[1].strip()
            initial = first[0] + "." if first else ""
            formatted.append(f"{last}, {initial}")
        else:
            formatted.append(a)
    return "; ".join(formatted)


def _fmt_count(n):
    """Format a number with commas."""
    if n is None:
        return "0"
    return f"{n:,}"


def display(papers, total, args):
    """Print results to terminal in terse, verbose, or brief mode."""
    if not papers:
        print("No results found.")
        return

    print(f"Showing {len(papers)} of {total:,} results\n")

    for p in papers:
        title = p.get("title", ["Untitled"])
        title = title[0] if isinstance(title, list) else title
        authors = p.get("author", [])
        year = p.get("year", "")
        bibcode = p.get("bibcode", "")
        abstract = p.get("abstract", "")
        cites = p.get("citation_count")
        reads = p.get("read_count")

        if args.brief:
            author = _author_compact(authors)
            print(f"[{bibcode}] {title}")
            print(f"{author} | {year} | {_fmt_count(cites)} citations")
            print(abstract)
            print()

        elif args.verbose:
            print(f"[{bibcode}] {title}")
            print(f"  Authors: {_author_verbose(authors)}")
            print(f"  Year: {year}  |  Citations: {_fmt_count(cites)}  |  Reads: {_fmt_count(reads)}")
            doi = p.get("doi")
            if doi:
                doi_str = doi[0] if isinstance(doi, list) else doi
                print(f"  DOI: {doi_str}")
            pub = p.get("pub")
            if pub:
                print(f"  Journal: {pub}")
            pubdate = p.get("pubdate", "")
            if pubdate:
                print(f"  Pub date: {pubdate}")
            doctype = p.get("doctype", "")
            if doctype:
                print(f"  Type: {doctype}")
            keywords = p.get("keyword", [])
            if keywords:
                print(f"  Keywords: {', '.join(keywords[:10])}")
            print(f"  Bibcode: {bibcode}")
            print()
            if abstract:
                for line in textwrap.wrap(abstract, width=90):
                    print(f"  {line}")
                print()

        else:
            # Default terse
            author = _author_short(authors)
            print(f"[{bibcode}] {title}")
            print(f"  {author} | {year} | {_fmt_count(cites)} citations")
            if abstract:
                print(f"  {_truncate(abstract)}")
            print()


def display_json(papers, total, args):
    """Print results as JSON."""
    output = {
        "total_results": total,
        "num_returned": len(papers),
        "papers": papers,
    }
    print(json.dumps(output, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Search NASA ADS / SciX papers from the command line.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("query", nargs="?", default=None, help="Search terms (free text)")
    parser.add_argument("-t", "--title", default=None, help="Search by title")
    parser.add_argument("-a", "--author", default=None, help="Search by author")
    parser.add_argument("-y", "--year", default=None, help="Filter by year (e.g. 2023 or 2020-2023)")
    parser.add_argument("-n", "--num", type=int, default=10, help="Number of results (default: 10)")
    parser.add_argument("--sort", choices=list(SORT_MAP.keys()), default=None, help="Sort order")
    parser.add_argument("--bibcode", default=None, help="Fetch a specific paper by bibcode")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show full details")
    parser.add_argument("-b", "--brief", action="store_true", help="Brief output with full abstract")
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    if not args.query and not args.title and not args.author and not args.bibcode:
        parser.print_help()
        sys.exit(1)

    try:
        client = SciXClient()
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    q, kwargs = build_query(args)

    try:
        result = client.search(q, fl=FIELDS, **kwargs)
    except SciXError as e:
        print(f"API error: {e}", file=sys.stderr)
        sys.exit(1)

    papers = result.get("response", {}).get("docs", [])
    total = result.get("response", {}).get("numFound", 0)

    if args.json:
        display_json(papers, total, args)
    else:
        display(papers, total, args)


if __name__ == "__main__":
    main()
