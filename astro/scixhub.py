"""SciX/ADS API wrapper.

Covers: Search, Export, Metrics, Libraries, Citation Helper, Resolver,
Reference, Oracle, Visualization, Objects, Graphics, Journals, Author
Affiliation, and Vault APIs.

Usage:
    from scixhub import SciXClient

    client = SciXClient(token="your-api-token")
    # or set ADS_API_TOKEN env var

    results = client.search('author:"Einstein" year:1905', fl="bibcode,title")
    print(results["response"]["docs"])
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

__all__ = ["SciXClient", "SciXError"]

DEFAULT_BASE_URL = "https://api.adsabs.harvard.edu/v1"


class SciXError(Exception):
    """API error with status code and response body."""

    def __init__(
        self,
        status_code: int,
        message: str,
        response: httpx.Response | None = None,
    ):
        self.status_code = status_code
        self.response = response
        super().__init__(f"HTTP {status_code}: {message}")


class SciXClient:
    """Client for the SciX/ADS API.

    Args:
        token: API bearer token. Falls back to ADS_API_TOKEN env var.
            Get one at https://ui.adsabs.harvard.edu/user/settings/token
        base_url: API base URL.
        timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        token: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
    ):
        self.token = token or os.environ.get("ADS_API_TOKEN", "")
        if not self.token:
            raise ValueError(
                "API token required. Pass token= or set ADS_API_TOKEN env var. "
                "Get one at https://ui.adsabs.harvard.edu/user/settings/token"
            )
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )

    # ── HTTP helpers ────────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        resp = self._client.request(method, path, **kwargs)
        if not resp.is_success:
            raise SciXError(resp.status_code, resp.text, response=resp)
        return resp

    def _get(self, path: str, **kwargs: Any) -> Any:
        return self._request("GET", path, **kwargs).json()

    def _post(self, path: str, **kwargs: Any) -> Any:
        return self._request("POST", path, **kwargs).json()

    def _put(self, path: str, **kwargs: Any) -> Any:
        return self._request("PUT", path, **kwargs).json()

    def _delete(self, path: str, **kwargs: Any) -> Any:
        return self._request("DELETE", path, **kwargs).json()

    @property
    def rate_limit(self) -> dict[str, str | None]:
        """Rate-limit info from the most recent response.

        Returns dict with keys: limit, remaining, reset.
        """
        last = self._client._transport  # noqa: not ideal but httpx has no last-response
        # Best accessed by inspecting response headers directly after a call
        return {
            "limit": None,
            "remaining": None,
            "reset": None,
        }

    @staticmethod
    def to_json(data: Any, indent: int = 2) -> str:
        """Serialize any API response to a JSON string."""
        return json.dumps(data, indent=indent, default=str)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SciXClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ── Search API ──────────────────────────────────────────────

    def search(
        self,
        q: str,
        *,
        fl: str = "bibcode",
        rows: int = 10,
        start: int = 0,
        sort: str | None = None,
        fq: str | list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Search for papers.

        Args:
            q: Query string (e.g. 'author:"Einstein" year:1905').
            fl: Comma-separated fields to return.
            rows: Number of results (max 2000).
            start: Pagination offset.
            sort: Sort order (e.g. 'citation_count desc').
            fq: Filter query or list of filter queries (max 10).

        Common fields for fl: bibcode, title, author, abstract, year,
            doi, citation_count, read_count, keyword, aff, pub, pubdate,
            doctype, identifier, orcid, property, esources.
        """
        params: dict[str, Any] = {
            "q": q,
            "fl": fl,
            "rows": rows,
            "start": start,
            **kwargs,
        }
        if sort:
            params["sort"] = sort
        if fq:
            params["fq"] = fq if isinstance(fq, list) else [fq]
        return self._get("/search/query", params=params)

    def bigquery(
        self,
        bibcodes: list[str],
        *,
        fl: str = "bibcode",
        rows: int = 2000,
        start: int = 0,
        sort: str | None = None,
    ) -> dict[str, Any]:
        """Batch lookup papers by bibcode list (~100 requests/day limit).

        Args:
            bibcodes: List of bibcodes to look up.
            fl: Fields to return.
            rows: Max results.
            start: Pagination offset.
            sort: Sort order.
        """
        params: dict[str, Any] = {
            "q": "*:*",
            "fq": "{!bitset}",
            "fl": fl,
            "rows": rows,
            "start": start,
        }
        if sort:
            params["sort"] = sort
        body = "bibcode\n" + "\n".join(bibcodes)
        return self._request(
            "POST",
            "/search/bigquery",
            params=params,
            content=body,
            headers={"Content-Type": "big-query/csv"},
        ).json()

    def qtree(self, q: str) -> dict[str, Any]:
        """Get the query abstract syntax tree (useful for debugging queries)."""
        return self._get("/search/qtree", params={"q": q})

    # ── Export API ──────────────────────────────────────────────

    EXPORT_FORMATS = frozenset({
        "bibtex", "bibtexabs", "ads", "endnote", "procite", "ris",
        "refworks", "medlars", "aastex", "icarus", "mnras", "soph",
        "dcxml", "refxml", "refabsxml", "votable", "rss", "ieee",
    })

    def export(
        self,
        bibcodes: list[str] | str,
        fmt: str = "bibtex",
        *,
        sort: str | None = None,
    ) -> str:
        """Export references in a standard format.

        Args:
            bibcodes: Single bibcode string or list.
            fmt: One of: bibtex, bibtexabs, ads, endnote, procite, ris,
                refworks, medlars, aastex, icarus, mnras, soph, dcxml,
                refxml, refabsxml, votable, rss, ieee.
            sort: Sort order for multi-bibcode export.

        Returns:
            Formatted bibliography string.
        """
        if isinstance(bibcodes, str):
            resp = self._get(f"/export/{fmt}/{bibcodes}")
        else:
            payload: dict[str, Any] = {"bibcode": bibcodes}
            if sort:
                payload["sort"] = sort
            resp = self._post(f"/export/{fmt}", json=payload)
        return resp.get("export", resp) if isinstance(resp, dict) else resp

    def export_csl(
        self,
        bibcodes: list[str],
        style: str,
        fmt: str = "text",
        *,
        journal_format: str | None = None,
        sort: str | None = None,
    ) -> str:
        """Export using Citation Style Language.

        Args:
            bibcodes: List of bibcodes.
            style: CSL style name.
            fmt: Output format (text, html).
            journal_format: Journal name format.
            sort: Sort order.
        """
        payload: dict[str, Any] = {
            "bibcode": bibcodes,
            "style": style,
            "format": fmt,
        }
        if journal_format:
            payload["journalformat"] = journal_format
        if sort:
            payload["sort"] = sort
        resp = self._post("/export/csl", json=payload)
        return resp.get("export", resp) if isinstance(resp, dict) else resp

    def export_custom(
        self,
        bibcodes: list[str],
        format_str: str,
        *,
        sort: str | None = None,
    ) -> str:
        """Export using a custom format string.

        Args:
            bibcodes: List of bibcodes.
            format_str: Custom format (e.g. '%A %Y %T').
            sort: Sort order.

        Format codes: %A (authors), %T (title), %Y (year), %J (journal),
            %V (volume), %P (page), %D (date), %B (abstract), %R (bibcode),
            %d (DOI), %K (keywords), %c (citation count), %U (URL).
        """
        payload: dict[str, Any] = {"bibcode": bibcodes, "format": format_str}
        if sort:
            payload["sort"] = sort
        resp = self._post("/export/custom", json=payload)
        return resp.get("export", resp) if isinstance(resp, dict) else resp

    # ── Metrics API ─────────────────────────────────────────────

    def metrics(
        self,
        bibcodes: list[str] | str,
        *,
        types: list[str] | None = None,
        histograms: list[str] | None = None,
    ) -> dict[str, Any]:
        """Get bibliometric statistics.

        Args:
            bibcodes: Single bibcode or list.
            types: Filter to: basic, citations, indicators, histograms, timeseries.
            histograms: Filter to: publications, reads, downloads, citations.
        """
        if isinstance(bibcodes, str):
            return self._get(f"/metrics/{bibcodes}")
        payload: dict[str, Any] = {"bibcodes": bibcodes}
        if types:
            payload["types"] = types
        if histograms:
            payload["histograms"] = histograms
        return self._post("/metrics", json=payload)

    # ── Libraries API (biblib) ──────────────────────────────────

    def list_libraries(
        self,
        *,
        start: int = 0,
        rows: int | None = None,
        sort: str = "date_created",
        order: str = "asc",
        access_type: str = "all",
    ) -> dict[str, Any]:
        """List all libraries for the authenticated user.

        Args:
            start: Pagination offset.
            rows: Number of libraries to return (None = all).
            sort: Sort by: date_created, date_last_modified, name.
            order: Sort direction: asc, desc.
            access_type: Filter: all, owner, collaborator.
        """
        params: dict[str, Any] = {
            "start": start,
            "sort": sort,
            "order": order,
            "access_type": access_type,
        }
        if rows is not None:
            params["rows"] = rows
        return self._get("/biblib/libraries", params=params)

    def create_library(
        self,
        name: str = "Untitled Library",
        description: str = "My ADS library",
        *,
        public: bool = False,
        bibcodes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new library.

        Returns dict with: name, id, description.
        """
        payload: dict[str, Any] = {
            "name": name,
            "description": description,
            "public": public,
        }
        if bibcodes:
            payload["bibcode"] = bibcodes
        return self._post("/biblib/libraries", json=payload)

    def get_library(
        self,
        library_id: str,
        *,
        start: int = 0,
        rows: int = 20,
        sort: str = "date desc",
        fl: str = "bibcode",
        raw: bool = False,
    ) -> dict[str, Any]:
        """Get library documents and metadata.

        Args:
            library_id: Library UUID.
            start: Pagination offset.
            rows: Documents per page.
            sort: Sort order.
            fl: Fields to return.
            raw: Return raw bibcodes regardless of ADS presence.
        """
        params: dict[str, Any] = {
            "start": start,
            "rows": rows,
            "sort": sort,
            "fl": fl,
            "raw": str(raw).lower(),
        }
        return self._get(f"/biblib/libraries/{library_id}", params=params)

    def update_library(
        self,
        library_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        public: bool | None = None,
    ) -> dict[str, Any]:
        """Update library metadata. Requires owner or admin permission."""
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        if public is not None:
            payload["public"] = public
        return self._put(f"/biblib/documents/{library_id}", json=payload)

    def delete_library(self, library_id: str) -> dict[str, Any]:
        """Permanently delete a library. Owner only."""
        return self._delete(f"/biblib/documents/{library_id}")

    def add_documents(
        self, library_id: str, bibcodes: list[str]
    ) -> dict[str, Any]:
        """Add documents to a library. Requires write permission."""
        return self._post(
            f"/biblib/documents/{library_id}",
            json={"bibcode": bibcodes, "action": "add"},
        )

    def remove_documents(
        self, library_id: str, bibcodes: list[str]
    ) -> dict[str, Any]:
        """Remove documents from a library. Requires write permission."""
        return self._post(
            f"/biblib/documents/{library_id}",
            json={"bibcode": bibcodes, "action": "remove"},
        )

    def library_add_by_query(
        self,
        library_id: str,
        *,
        q: str,
        fq: str | None = None,
        rows: int = 100,
        sort: str = "date desc",
        action: str = "add",
    ) -> dict[str, Any]:
        """Add or remove library documents via search query.

        Args:
            library_id: Library UUID.
            q: Search query.
            fq: Filter query.
            rows: Max documents to affect.
            sort: Sort order.
            action: 'add' or 'remove'.
        """
        params: dict[str, Any] = {"q": q, "rows": rows, "sort": sort}
        if fq:
            params["fq"] = fq
        return self._post(
            f"/biblib/query/{library_id}",
            json={"action": action, "params": params},
        )

    def library_set_operation(
        self,
        library_id: str,
        action: str,
        libraries: list[str],
        *,
        name: str | None = None,
        description: str | None = None,
        public: bool = False,
    ) -> dict[str, Any]:
        """Perform set operations between libraries.

        Args:
            library_id: Primary library UUID.
            action: union, intersection, difference, copy, or empty.
            libraries: Secondary library UUIDs.
            name: Name for new library (union/intersection/difference).
            description: Description for new library.
            public: Visibility of new library.
        """
        payload: dict[str, Any] = {
            "action": action,
            "libraries": libraries,
            "public": public,
        }
        if name:
            payload["name"] = name
        if description:
            payload["description"] = description
        return self._post(
            f"/biblib/libraries/operations/{library_id}", json=payload
        )

    def get_permissions(self, library_id: str) -> Any:
        """View permission assignments for a library."""
        return self._get(f"/biblib/permissions/{library_id}")

    def set_permissions(
        self,
        library_id: str,
        email: str,
        *,
        read: bool = False,
        write: bool = False,
        admin: bool = False,
    ) -> dict[str, Any]:
        """Grant or revoke library permissions. Requires owner or admin."""
        return self._post(
            f"/biblib/permissions/{library_id}",
            json={
                "email": email,
                "permission": {"read": read, "write": write, "admin": admin},
            },
        )

    def transfer_library(
        self, library_id: str, email: str
    ) -> dict[str, Any]:
        """Transfer library ownership. Owner only."""
        return self._post(
            f"/biblib/transfer/{library_id}", json={"email": email}
        )

    def add_note(
        self, library_id: str, document_id: str, content: str
    ) -> dict[str, Any]:
        """Add a note to a document in a library."""
        return self._post(
            f"/biblib/notes/{library_id}/{document_id}",
            json={"content": content},
        )

    def get_note(
        self, library_id: str, document_id: str
    ) -> dict[str, Any]:
        """Get note for a document."""
        return self._get(f"/biblib/notes/{library_id}/{document_id}")

    def update_note(
        self, library_id: str, document_id: str, content: str
    ) -> dict[str, Any]:
        """Update a document note."""
        return self._put(
            f"/biblib/notes/{library_id}/{document_id}",
            json={"content": content},
        )

    def delete_note(
        self, library_id: str, document_id: str
    ) -> dict[str, Any]:
        """Delete a document note."""
        return self._delete(f"/biblib/notes/{library_id}/{document_id}")

    # ── Citation Helper ─────────────────────────────────────────

    def citation_helper(self, bibcodes: list[str]) -> Any:
        """Suggest up to 10 missing citations using co-citation analysis.

        Returns list of dicts with: bibcode, title, author, score.
        """
        return self._post("/citation_helper", json={"bibcodes": bibcodes})

    # ── Resolver API ────────────────────────────────────────────

    def resolve(
        self, bibcode: str, link_type: str | None = None
    ) -> dict[str, Any]:
        """Get external resource links for a bibcode.

        Args:
            bibcode: Paper bibcode.
            link_type: Optional specific link type (e.g. 'pub_pdf',
                'eprint_pdf', 'citations', 'references', 'esource',
                'data', 'simbad', 'ned', 'vizier').
        """
        path = f"/resolver/{bibcode}"
        if link_type:
            path += f"/{link_type}"
        return self._get(path)

    # ── Reference API ───────────────────────────────────────────

    def resolve_references(self, references: list[str]) -> dict[str, Any]:
        """Resolve free-text reference strings to bibcodes.

        Args:
            references: List of reference strings, e.g.
                ["Huchra, J. et al. 1992, ApJS, 199, 26"]

        Returns dict with 'resolved' containing: bibcode, refstring, score.
        """
        return self._post("/reference/text", json={"reference": references})

    def parse_references(self, references: list[str]) -> dict[str, Any]:
        """Parse reference strings into structured components (no resolution).

        Returns dict with 'parsed' containing: authors, year, journal, etc.
        """
        return self._post("/reference/parse", json={"reference": references})

    # ── Oracle API ──────────────────────────────────────────────

    def match_document(
        self,
        *,
        abstract: str,
        title: str,
        author: str,
        year: int,
        doctype: str = "article",
        doi: str | None = None,
        mustmatch: bool = False,
        match_doctype: list[str] | None = None,
    ) -> dict[str, Any]:
        """Find the matching ADS bibcode for given document metadata.

        Args:
            abstract: Paper abstract.
            title: Paper title.
            author: Authors as 'Last, First; Last, First'.
            year: Publication year.
            doctype: Document type.
            doi: Optional DOI.
            mustmatch: If True, require ADS to have this paper.
            match_doctype: Expected doctypes of matched record.
        """
        payload: dict[str, Any] = {
            "abstract": abstract,
            "title": title,
            "author": author,
            "year": year,
            "doctype": doctype,
            "mustmatch": mustmatch,
        }
        if doi:
            payload["doi"] = doi
        if match_doctype:
            payload["match_doctype"] = match_doctype
        return self._post("/oracle/matchdoc", json=payload)

    def recommendations(
        self,
        function: str = "similar",
        *,
        sort: str | None = None,
        num_docs: int = 10,
        top_n_reads: int = 50,
        cutoff_days: int = 365,
    ) -> dict[str, Any]:
        """Get paper recommendations based on reading history.

        Args:
            function: similar, trending, reviews, or useful.
            sort: Sort order for recommendations.
            num_docs: Number of recommendations.
            top_n_reads: Input records for generating recommendations.
            cutoff_days: Days back to consider.
        """
        payload: dict[str, Any] = {
            "function": function,
            "num_docs": num_docs,
            "top_n_reads": top_n_reads,
            "cutoff_days": cutoff_days,
        }
        if sort:
            payload["sort"] = sort
        return self._post("/oracle/readhist", json=payload)

    # ── Visualization API ───────────────────────────────────────

    def author_network(self, bibcodes: list[str]) -> dict[str, Any]:
        """Generate author collaboration network data."""
        return self._post("/vis/author-network", json={"bibcodes": bibcodes})

    def paper_network(self, bibcodes: list[str]) -> dict[str, Any]:
        """Generate paper relationship network data."""
        return self._post("/vis/paper-network", json={"bibcodes": bibcodes})

    def word_cloud(self, bibcodes: list[str]) -> dict[str, Any]:
        """Generate word cloud data from papers (max 500 records)."""
        return self._post("/vis/word-cloud", json={"bibcodes": bibcodes})

    # ── Objects API ─────────────────────────────────────────────

    def resolve_objects(
        self,
        identifiers: list[str],
        source: str = "simbad",
    ) -> dict[str, Any]:
        """Resolve astronomical object names to canonical IDs.

        Args:
            identifiers: Object names (e.g. ['M31', 'NGC 1275']).
            source: 'simbad' or 'ned'.
        """
        return self._post(
            "/objects",
            json={"source": source, "identifiers": identifiers},
        )

    def expand_object_query(self, query: str) -> dict[str, Any]:
        """Expand an object query with SIMBAD/NED identifiers for Solr."""
        return self._post("/objects/query", json={"query": [query]})

    # ── Graphics API ────────────────────────────────────────────

    def graphics(self, bibcode: str) -> Any:
        """Get figures and graphics metadata for a paper."""
        return self._get(f"/graphics/{bibcode}")

    # ── Journals API ────────────────────────────────────────────

    def journal_summary(self, bibstem: str) -> dict[str, Any]:
        """Get comprehensive journal metadata by bibstem (case-sensitive)."""
        return self._get(f"/journals/summary/{bibstem}")

    def search_journals(self, name: str) -> dict[str, Any]:
        """Search journals by name or abbreviation."""
        return self._get(f"/journals/journal/{name}")

    def journal_holdings(
        self, bibstem: str, volume: str
    ) -> dict[str, Any]:
        """Get available electronic full-text sources for a journal volume."""
        return self._get(f"/journals/holdings/{bibstem}/{volume}")

    def journal_issn(self, issn: str) -> dict[str, Any]:
        """Look up journal by ISSN."""
        return self._get(f"/journals/issn/{issn}")

    def journal_refsource(self, bibstem: str) -> dict[str, Any]:
        """Get citation data sources per volume for a journal."""
        return self._get(f"/journals/refsource/{bibstem}")

    # ── Author Affiliation API ──────────────────────────────────

    def author_affiliation(
        self,
        bibcodes: list[str],
        *,
        maxauthor: int | None = None,
        numyears: int | None = None,
    ) -> dict[str, Any]:
        """Generate author affiliation report.

        Args:
            bibcodes: List of bibcodes.
            maxauthor: Limit to first N authors per paper.
            numyears: Retrieve affiliations from past N years.
        """
        payload: dict[str, Any] = {"bibcodes": bibcodes}
        if maxauthor is not None:
            payload["maxauthor"] = maxauthor
        if numyears is not None:
            payload["numyears"] = numyears
        return self._post("/author-affiliation/search", json=payload)

    # ── Vault API (stored queries & notifications) ──────────────

    def save_query(self, q: str, **kwargs: Any) -> dict[str, Any]:
        """Save a query for later execution. Returns dict with qid."""
        return self._post("/vault/query", json={"q": q, **kwargs})

    def get_stored_query(self, qid: str) -> dict[str, Any]:
        """Retrieve a stored query by ID."""
        return self._get(f"/vault/query/{qid}")

    def execute_stored_query(self, qid: str) -> dict[str, Any]:
        """Execute a stored query and return search results."""
        return self._get(f"/vault/execute_query/{qid}")

    def list_notifications(self) -> dict[str, Any]:
        """List all myADS notifications."""
        return self._get("/vault/notifications")

    def create_notification(
        self,
        *,
        type: str,
        name: str,
        frequency: str = "weekly",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Create a myADS notification.

        Args:
            type: 'template' or 'query'.
            name: Notification name.
            frequency: 'daily' or 'weekly'.
            **kwargs: Additional fields (qid, template, classes, data).
        """
        payload: dict[str, Any] = {
            "type": type,
            "name": name,
            "frequency": frequency,
            **kwargs,
        }
        return self._post("/vault/notifications", json=payload)

    def get_notification(self, myads_id: str) -> dict[str, Any]:
        """Get a specific myADS notification."""
        return self._get(f"/vault/notifications/{myads_id}")

    def update_notification(
        self, myads_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Update a myADS notification."""
        return self._put(f"/vault/notifications/{myads_id}", json=kwargs)

    def delete_notification(self, myads_id: str) -> dict[str, Any]:
        """Delete a myADS notification."""
        return self._delete(f"/vault/notifications/{myads_id}")
