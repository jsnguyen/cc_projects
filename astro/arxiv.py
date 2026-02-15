#!/usr/bin/env python3
"""CLI tool to search arXiv papers via the public API with local caching."""

import argparse
import hashlib
import json
import sqlite3
import sys
import textwrap
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

ARXIV_API_URL = "http://export.arxiv.org/api/query"
CACHE_DB = Path.home() / ".arxiv_cache.db"
CACHE_TTL = 3600  # 1 hour
MAX_RESULTS_CAP = 50
ATOM = "{http://www.w3.org/2005/Atom}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"

_last_request_time = 0.0

CATEGORIES = {
    "astro-ph": {
        "name": "Astrophysics",
        "subcats": {
            "astro-ph.CO": "Cosmology and Nongalactic Astrophysics",
            "astro-ph.EP": "Earth and Planetary Astrophysics",
            "astro-ph.GA": "Astrophysics of Galaxies",
            "astro-ph.HE": "High Energy Astrophysical Phenomena",
            "astro-ph.IM": "Instrumentation and Methods for Astrophysics",
            "astro-ph.SR": "Solar and Stellar Astrophysics",
        },
    },
    "cond-mat": {
        "name": "Condensed Matter",
        "subcats": {
            "cond-mat.dis-nn": "Disordered Systems and Neural Networks",
            "cond-mat.mes-hall": "Mesoscale and Nanoscale Physics",
            "cond-mat.mtrl-sci": "Materials Science",
            "cond-mat.other": "Other Condensed Matter",
            "cond-mat.quant-gas": "Quantum Gases",
            "cond-mat.soft": "Soft Condensed Matter",
            "cond-mat.stat-mech": "Statistical Mechanics",
            "cond-mat.str-el": "Strongly Correlated Electrons",
            "cond-mat.supr-con": "Superconductivity",
        },
    },
    "cs": {
        "name": "Computer Science",
        "subcats": {
            "cs.AI": "Artificial Intelligence",
            "cs.AR": "Hardware Architecture",
            "cs.CC": "Computational Complexity",
            "cs.CE": "Computational Engineering, Finance, and Science",
            "cs.CG": "Computational Geometry",
            "cs.CL": "Computation and Language",
            "cs.CR": "Cryptography and Security",
            "cs.CV": "Computer Vision and Pattern Recognition",
            "cs.CY": "Computers and Society",
            "cs.DB": "Databases",
            "cs.DC": "Distributed, Parallel, and Cluster Computing",
            "cs.DL": "Digital Libraries",
            "cs.DM": "Discrete Mathematics",
            "cs.DS": "Data Structures and Algorithms",
            "cs.ET": "Emerging Technologies",
            "cs.FL": "Formal Languages and Automata Theory",
            "cs.GL": "General Literature",
            "cs.GR": "Graphics",
            "cs.GT": "Computer Science and Game Theory",
            "cs.HC": "Human-Computer Interaction",
            "cs.IR": "Information Retrieval",
            "cs.IT": "Information Theory",
            "cs.LG": "Machine Learning",
            "cs.LO": "Logic in Computer Science",
            "cs.MA": "Multiagent Systems",
            "cs.MM": "Multimedia",
            "cs.MS": "Mathematical Software",
            "cs.NA": "Numerical Analysis",
            "cs.NE": "Neural and Evolutionary Computing",
            "cs.NI": "Networking and Internet Architecture",
            "cs.OH": "Other Computer Science",
            "cs.OS": "Operating Systems",
            "cs.PF": "Performance",
            "cs.PL": "Programming Languages",
            "cs.RO": "Robotics",
            "cs.SC": "Symbolic Computation",
            "cs.SD": "Sound",
            "cs.SE": "Software Engineering",
            "cs.SI": "Social and Information Networks",
            "cs.SY": "Systems and Control",
        },
    },
    "econ": {
        "name": "Economics",
        "subcats": {
            "econ.EM": "Econometrics",
            "econ.GN": "General Economics",
            "econ.TH": "Theoretical Economics",
        },
    },
    "eess": {
        "name": "Electrical Engineering and Systems Science",
        "subcats": {
            "eess.AS": "Audio and Speech Processing",
            "eess.IV": "Image and Video Processing",
            "eess.SP": "Signal Processing",
            "eess.SY": "Systems and Control",
        },
    },
    "gr-qc": {
        "name": "General Relativity and Quantum Cosmology",
        "subcats": {},
    },
    "hep-ex": {
        "name": "High Energy Physics - Experiment",
        "subcats": {},
    },
    "hep-lat": {
        "name": "High Energy Physics - Lattice",
        "subcats": {},
    },
    "hep-ph": {
        "name": "High Energy Physics - Phenomenology",
        "subcats": {},
    },
    "hep-th": {
        "name": "High Energy Physics - Theory",
        "subcats": {},
    },
    "math": {
        "name": "Mathematics",
        "subcats": {
            "math.AC": "Commutative Algebra",
            "math.AG": "Algebraic Geometry",
            "math.AP": "Analysis of PDEs",
            "math.AT": "Algebraic Topology",
            "math.CA": "Classical Analysis and ODEs",
            "math.CO": "Combinatorics",
            "math.CT": "Category Theory",
            "math.CV": "Complex Variables",
            "math.DG": "Differential Geometry",
            "math.DS": "Dynamical Systems",
            "math.FA": "Functional Analysis",
            "math.GM": "General Mathematics",
            "math.GN": "General Topology",
            "math.GR": "Group Theory",
            "math.GT": "Geometric Topology",
            "math.HO": "History and Overview",
            "math.IT": "Information Theory",
            "math.KT": "K-Theory and Homology",
            "math.LO": "Logic",
            "math.MG": "Metric Geometry",
            "math.MP": "Mathematical Physics",
            "math.NA": "Numerical Analysis",
            "math.NT": "Number Theory",
            "math.OA": "Operator Algebras",
            "math.OC": "Optimization and Control",
            "math.PR": "Probability",
            "math.QA": "Quantum Algebra",
            "math.RA": "Rings and Algebras",
            "math.RT": "Representation Theory",
            "math.SG": "Symplectic Geometry",
            "math.SP": "Spectral Theory",
            "math.ST": "Statistics Theory",
        },
    },
    "math-ph": {
        "name": "Mathematical Physics",
        "subcats": {},
    },
    "nlin": {
        "name": "Nonlinear Sciences",
        "subcats": {
            "nlin.AO": "Adaptation and Self-Organizing Systems",
            "nlin.CD": "Chaotic Dynamics",
            "nlin.CG": "Cellular Automata and Lattice Gases",
            "nlin.PS": "Pattern Formation and Solitons",
            "nlin.SI": "Exactly Solvable and Integrable Systems",
        },
    },
    "nucl-ex": {
        "name": "Nuclear Experiment",
        "subcats": {},
    },
    "nucl-th": {
        "name": "Nuclear Theory",
        "subcats": {},
    },
    "physics": {
        "name": "Physics",
        "subcats": {
            "physics.acc-ph": "Accelerator Physics",
            "physics.ao-ph": "Atmospheric and Oceanic Physics",
            "physics.app-ph": "Applied Physics",
            "physics.atm-clus": "Atomic and Molecular Clusters",
            "physics.atom-ph": "Atomic Physics",
            "physics.bio-ph": "Biological Physics",
            "physics.chem-ph": "Chemical Physics",
            "physics.class-ph": "Classical Physics",
            "physics.comp-ph": "Computational Physics",
            "physics.data-an": "Data Analysis, Statistics and Probability",
            "physics.ed-ph": "Physics Education",
            "physics.flu-dyn": "Fluid Dynamics",
            "physics.gen-ph": "General Physics",
            "physics.geo-ph": "Geophysics",
            "physics.hist-ph": "History and Philosophy of Physics",
            "physics.ins-det": "Instrumentation and Detectors",
            "physics.med-ph": "Medical Physics",
            "physics.optics": "Optics",
            "physics.plasm-ph": "Plasma Physics",
            "physics.pop-ph": "Popular Physics",
            "physics.soc-ph": "Physics and Society",
            "physics.space-ph": "Space Physics",
        },
    },
    "q-bio": {
        "name": "Quantitative Biology",
        "subcats": {
            "q-bio.BM": "Biomolecules",
            "q-bio.CB": "Cell Behavior",
            "q-bio.GN": "Genomics",
            "q-bio.MN": "Molecular Networks",
            "q-bio.NC": "Neurons and Cognition",
            "q-bio.OT": "Other Quantitative Biology",
            "q-bio.PE": "Populations and Evolution",
            "q-bio.QM": "Quantitative Methods",
            "q-bio.SC": "Subcellular Processes",
            "q-bio.TO": "Tissues and Organs",
        },
    },
    "q-fin": {
        "name": "Quantitative Finance",
        "subcats": {
            "q-fin.CP": "Computational Finance",
            "q-fin.EC": "Economics",
            "q-fin.GN": "General Finance",
            "q-fin.MF": "Mathematical Finance",
            "q-fin.PM": "Portfolio Management",
            "q-fin.PR": "Pricing of Securities",
            "q-fin.RM": "Risk Management",
            "q-fin.ST": "Statistical Finance",
            "q-fin.TR": "Trading and Market Microstructure",
        },
    },
    "quant-ph": {
        "name": "Quantum Physics",
        "subcats": {},
    },
    "stat": {
        "name": "Statistics",
        "subcats": {
            "stat.AP": "Applications",
            "stat.CO": "Computation",
            "stat.ME": "Methodology",
            "stat.ML": "Machine Learning",
            "stat.OT": "Other Statistics",
            "stat.TH": "Statistics Theory",
        },
    },
}


def _all_valid_categories():
    """Return a set of all valid category strings (groups and sub-categories)."""
    valid = set(CATEGORIES.keys())
    for group in CATEGORIES.values():
        valid.update(group["subcats"].keys())
    return valid


def list_categories(group=None):
    """Print category listing."""
    if group:
        if group not in CATEGORIES:
            print(f"Unknown category group: {group}")
            print(f"Valid groups: {', '.join(sorted(CATEGORIES.keys()))}")
            sys.exit(1)
        info = CATEGORIES[group]
        print(f"{group} — {info['name']}")
        if info["subcats"]:
            for cat, desc in sorted(info["subcats"].items()):
                print(f"  {cat:20s} {desc}")
        else:
            print(f"  (no sub-categories; use '{group}' directly)")
    else:
        for grp, info in sorted(CATEGORIES.items()):
            count = len(info["subcats"])
            suffix = f"({count} sub-categories)" if count else "(standalone)"
            print(f"  {grp:12s} {info['name']:50s} {suffix}")


def _get_cache_db():
    conn = sqlite3.connect(CACHE_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cache "
        "(query_key TEXT PRIMARY KEY, response TEXT, timestamp REAL)"
    )
    return conn


def cache_get(key):
    conn = _get_cache_db()
    row = conn.execute(
        "SELECT response, timestamp FROM cache WHERE query_key = ?", (key,)
    ).fetchone()
    conn.close()
    if row and (time.time() - row[1]) < CACHE_TTL:
        return row[0]
    return None


def cache_set(key, response):
    conn = _get_cache_db()
    conn.execute(
        "INSERT OR REPLACE INTO cache (query_key, response, timestamp) VALUES (?, ?, ?)",
        (key, response, time.time()),
    )
    conn.commit()
    conn.close()


def clear_cache():
    conn = _get_cache_db()
    conn.execute("DELETE FROM cache")
    conn.commit()
    conn.close()
    print("Cache cleared.")


def build_query(args):
    """Construct the API URL from CLI args. Returns (url, params_dict)."""
    params = {}

    if args.id:
        params["id_list"] = args.id
        if args.query:
            params["search_query"] = f"all:{args.query}"
        return ARXIV_API_URL, params

    parts = []

    if args.title:
        parts.append(f"ti:{args.title}")
    if args.author:
        parts.append(f"au:{args.author}")
    if args.query:
        parts.append(f"all:{args.query}")

    # Category filter
    if args.category:
        cat = args.category
        valid = _all_valid_categories()
        if cat not in valid:
            print(f"Unknown category: {cat}")
            print(f"Use --list-categories to see valid categories.")
            sys.exit(1)
        if cat in CATEGORIES and CATEGORIES[cat]["subcats"]:
            # Group: OR together all sub-categories
            sub_parts = [f"cat:{sc}" for sc in CATEGORIES[cat]["subcats"]]
            cat_expr = " OR ".join(sub_parts)
            parts.append(f"({cat_expr})")
        else:
            parts.append(f"cat:{cat}")

    if not parts:
        print("No search terms provided.")
        sys.exit(1)

    params["search_query"] = " AND ".join(parts)
    params["start"] = args.start
    params["max_results"] = min(args.num, MAX_RESULTS_CAP)

    sort_map = {"relevance": "relevance", "date": "submittedDate", "updated": "lastUpdatedDate"}
    if args.sort:
        params["sortBy"] = sort_map.get(args.sort, args.sort)
        params["sortOrder"] = "descending"

    return ARXIV_API_URL, params


def fetch(url, params, no_cache=False):
    """GET with caching and rate-limit compliance."""
    global _last_request_time

    # Build cache key from full request
    cache_key = hashlib.sha256(f"{url}?{sorted(params.items())}".encode()).hexdigest()

    if not no_cache:
        cached = cache_get(cache_key)
        if cached:
            return cached

    # Rate limit: wait at least 3s between requests
    elapsed = time.time() - _last_request_time
    if elapsed < 3.0:
        time.sleep(3.0 - elapsed)

    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    _last_request_time = time.time()

    text = resp.text
    cache_set(cache_key, text)
    return text


def parse_feed(xml_text):
    """Parse Atom XML into list of paper dicts and total results count."""
    root = ET.fromstring(xml_text)

    total_str = root.findtext(f"{OPENSEARCH}totalResults", "0")
    total = int(total_str)

    papers = []
    for entry in root.findall(f"{ATOM}entry"):
        # Extract arXiv ID from the <id> URL
        raw_id = entry.findtext(f"{ATOM}id", "")
        arxiv_id = raw_id.split("/abs/")[-1] if "/abs/" in raw_id else raw_id

        title = entry.findtext(f"{ATOM}title", "").replace("\n", " ").strip()
        title = " ".join(title.split())  # collapse whitespace

        summary = entry.findtext(f"{ATOM}summary", "").strip()
        summary = " ".join(summary.split())

        published = entry.findtext(f"{ATOM}published", "")[:10]
        updated = entry.findtext(f"{ATOM}updated", "")[:10]

        authors = [
            a.findtext(f"{ATOM}name", "") for a in entry.findall(f"{ATOM}author")
        ]

        # Categories
        categories = [
            c.get("term", "")
            for c in entry.findall(f"{ATOM}category")
            if c.get("term")
        ]
        primary_cat_el = entry.find(f"{ARXIV_NS}primary_category")
        primary_cat = primary_cat_el.get("term", "") if primary_cat_el is not None else ""
        if not primary_cat and categories:
            primary_cat = categories[0]

        # Links
        pdf_link = ""
        for link in entry.findall(f"{ATOM}link"):
            if link.get("title") == "pdf":
                pdf_link = link.get("href", "")
                break

        journal_ref = entry.findtext(f"{ARXIV_NS}journal_ref", "")
        comment = entry.findtext(f"{ARXIV_NS}comment", "")

        papers.append({
            "id": arxiv_id,
            "title": title,
            "summary": summary,
            "authors": authors,
            "published": published,
            "updated": updated,
            "primary_category": primary_cat,
            "categories": categories,
            "pdf_link": pdf_link,
            "journal_ref": journal_ref,
            "comment": comment,
        })

    return papers, total


def _author_short(authors):
    """Format as 'LastName et al.' for terse display."""
    if not authors:
        return "Unknown"
    first = authors[0].split()[-1]
    if len(authors) == 1:
        return first
    return f"{first} et al."


def _author_compact(authors):
    """Format as 'FirstAuthor+N' for brief display."""
    if not authors:
        return "Unknown"
    first = authors[0].split()[-1]
    if len(authors) == 1:
        return first
    return f"{first}+{len(authors) - 1}"


def _truncate(text, length=200):
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0] + "..."


def display_json(papers, total, args):
    """Print results as JSON."""
    output = {
        "total_results": total,
        "start": args.start,
        "num_returned": len(papers),
        "papers": papers,
    }
    print(json.dumps(output, indent=2))


def display(papers, total, args):
    """Print results to terminal."""
    if not papers:
        print("No results found.")
        return

    start = args.start
    end = start + len(papers)
    print(f"Showing {start + 1}-{end} of {total:,} results\n")

    for i, p in enumerate(papers):
        if args.brief:
            # Brief / agent-friendly format
            idx = f"[{start + i + 1}/{min(total, args.num)}]"
            author = _author_compact(p["authors"])
            print(f"{idx} {p['id']} | {p['title']}")
            print(f"{author} | {p['primary_category']} | {p['published']}")
            print(p["summary"])
            print()
        elif args.verbose:
            # Verbose format
            print(f"[{p['id']}] {p['title']}")
            print(f"  Authors: {', '.join(p['authors'])}")
            print(f"  Category: {p['primary_category']}  |  Published: {p['published']}  |  Updated: {p['updated']}")
            if p["journal_ref"]:
                print(f"  Journal: {p['journal_ref']}")
            if p["comment"]:
                print(f"  Comment: {p['comment']}")
            print(f"  PDF: {p['pdf_link']}")
            print()
            # Full abstract with wrapping
            for line in textwrap.wrap(p["summary"], width=90):
                print(f"  {line}")
            print()
        else:
            # Default terse format
            print(f"[{p['id']}] {p['title']}")
            author = _author_short(p["authors"])
            print(f"  {author} | {p['primary_category']} | {p['published']}")
            print(f"  {_truncate(p['summary'])}")
            print()


def main():
    parser = argparse.ArgumentParser(
        description="Search arXiv papers from the command line.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("query", nargs="?", default=None, help="Search terms (searches all fields)")
    parser.add_argument("-t", "--title", default=None, help="Search by title")
    parser.add_argument("-a", "--author", default=None, help="Search by author")
    parser.add_argument("-c", "--category", default=None, help="Filter by category (e.g. cs, cs.AI)")
    parser.add_argument("-n", "--num", type=int, default=10, help="Number of results (default: 10, max: 50)")
    parser.add_argument("--start", type=int, default=0, help="Offset for pagination (default: 0)")
    parser.add_argument("--sort", choices=["relevance", "date", "updated"], default=None, help="Sort order")
    parser.add_argument("--id", default=None, help="Fetch a specific paper by arXiv ID")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show full details")
    parser.add_argument("-b", "--brief", action="store_true", help="Brief agent-friendly output")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--no-cache", action="store_true", help="Bypass cache for this query")
    parser.add_argument("--clear-cache", action="store_true", help="Clear the local cache")
    parser.add_argument("--list-categories", nargs="?", const="__all__", default=None,
                        metavar="GROUP", help="List category groups, or sub-categories for a group")

    args = parser.parse_args()

    if args.clear_cache:
        clear_cache()
        return

    if args.list_categories is not None:
        group = None if args.list_categories == "__all__" else args.list_categories
        list_categories(group)
        return

    if not args.query and not args.title and not args.author and not args.id:
        parser.print_help()
        sys.exit(1)

    url, params = build_query(args)
    xml_text = fetch(url, params, no_cache=args.no_cache)
    papers, total = parse_feed(xml_text)
    if args.json:
        display_json(papers, total, args)
    else:
        display(papers, total, args)


if __name__ == "__main__":
    main()
