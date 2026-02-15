"""SIMBAD astronomical database API wrapper.

Uses the TAP/ADQL endpoint at CDS Strasbourg. No authentication required.

Usage:
    from simbad import SimbadClient

    with SimbadClient() as client:
        result = client.query_id("M31")
        print(result)
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

__all__ = ["SimbadClient", "SimbadError"]

TAP_URL = "https://simbad.cds.unistra.fr/simbad/sim-tap/sync"
SIM_BASIC_URL = "https://simbad.cds.unistra.fr/simbad/sim-basic"

BASIC_FIELDS = (
    "basic.main_id, basic.ra, basic.dec, basic.otype, basic.otype_txt, "
    "basic.plx_value, basic.rvz_radvel, basic.rvz_redshift, basic.rvz_err, "
    "basic.sp_type, basic.nbref"
)

EXTENDED_FIELDS = (
    f"{BASIC_FIELDS}, basic.pmra, basic.pmdec, "
    "basic.pm_err_maj, basic.pm_err_min, basic.plx_err, "
    "basic.morph_type, basic.galdim_majaxis, basic.galdim_minaxis"
)


def _escape(identifier: str) -> str:
    return identifier.replace("'", "''")


class SimbadError(Exception):
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


class SimbadClient:
    """Client for the SIMBAD TAP/ADQL API.

    Args:
        timeout: Request timeout in seconds.
    """

    def __init__(self, timeout: float = 30.0):
        self._client = httpx.Client(timeout=timeout)

    def _query(self, adql: str) -> list[dict[str, Any]]:
        """Execute an ADQL query and return list of dicts."""
        resp = self._client.get(
            TAP_URL,
            params={
                "request": "doQuery",
                "lang": "adql",
                "format": "json",
                "query": adql,
            },
        )
        if not resp.is_success:
            raise SimbadError(resp.status_code, resp.text, response=resp)
        return self._to_dicts(resp.json())

    @staticmethod
    def _to_dicts(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Convert TAP JSON response to list of dicts."""
        columns = [col["name"] for col in payload.get("metadata", [])]
        return [dict(zip(columns, row)) for row in payload.get("data", [])]

    @staticmethod
    def to_json(data: Any, indent: int = 2) -> str:
        return json.dumps(data, indent=indent, default=str)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SimbadClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ── Queries ────────────────────────────────────────────────

    def query_id(self, identifier: str) -> list[dict[str, Any]]:
        """Look up an object by name/identifier.

        Uses the ident table to avoid SIMBAD's internal space-padding issues.
        Returns extended fields (coordinates, PM, parallax, RV, morphology).
        """
        eid = _escape(identifier)
        adql = (
            f"SELECT TOP 1 {EXTENDED_FIELDS} "
            f"FROM basic JOIN ident ON basic.oid = ident.oidref "
            f"WHERE ident.id = '{eid}'"
        )
        return self._query(adql)

    def query_cone(
        self,
        ra: float,
        dec: float,
        radius: float,
        *,
        limit: int = 20,
        otype: str | None = None,
    ) -> list[dict[str, Any]]:
        """Cone search around a position.

        Args:
            ra: Right ascension in degrees.
            dec: Declination in degrees.
            radius: Search radius in degrees.
            limit: Maximum results.
            otype: Filter by object type (e.g. 'Psr', 'Y*O').
        """
        where = (
            f"WHERE CONTAINS(POINT('ICRS', basic.ra, basic.dec), "
            f"CIRCLE('ICRS', {ra}, {dec}, {radius})) = 1"
        )
        if otype:
            where += f" AND basic.otype = '{_escape(otype)}'"
        adql = (
            f"SELECT TOP {limit} {BASIC_FIELDS} "
            f"FROM basic {where} "
            f"ORDER BY nbref DESC"
        )
        return self._query(adql)

    def query_criteria(
        self,
        *,
        otype: str | None = None,
        sp_type: str | None = None,
        where: str | None = None,
        limit: int = 20,
        order_by: str = "nbref DESC",
    ) -> list[dict[str, Any]]:
        """Filter search by object type, spectral type, or custom WHERE clause.

        Args:
            otype: Object type code (e.g. 'Psr', 'QSO').
            sp_type: Spectral type pattern (SQL LIKE, e.g. 'O%').
            where: Raw WHERE clause fragment.
            limit: Maximum results.
            order_by: ORDER BY clause.
        """
        conditions = []
        if otype:
            conditions.append(f"basic.otype = '{_escape(otype)}'")
        if sp_type:
            conditions.append(f"basic.sp_type LIKE '{_escape(sp_type)}'")
        if where:
            conditions.append(where)
        if not conditions:
            raise ValueError("At least one filter (otype, sp_type, or where) required")
        where_clause = " AND ".join(conditions)
        adql = (
            f"SELECT TOP {limit} {BASIC_FIELDS} "
            f"FROM basic WHERE {where_clause} "
            f"ORDER BY {order_by}"
        )
        return self._query(adql)

    def get_identifiers(self, identifier: str) -> list[str]:
        """Get all known aliases for an object.

        Two-step: resolve oid from ident table, then fetch all ident entries.
        """
        eid = _escape(identifier)
        rows = self._query(
            f"SELECT oidref FROM ident WHERE id = '{eid}'"
        )
        if not rows:
            return []
        oid = rows[0]["oidref"]
        rows = self._query(
            f"SELECT id FROM ident WHERE oidref = {oid} ORDER BY id"
        )
        return [r["id"] for r in rows]

    def get_fluxes(self, identifier: str) -> list[dict[str, Any]]:
        """Get photometry for an object from the flux table (includes errors).

        Returns list of dicts with keys: filter, flux, flux_err, qual.
        """
        eid = _escape(identifier)
        adql = (
            f"SELECT filter, flux, flux_err, qual "
            f"FROM flux JOIN ident ON flux.oidref = ident.oidref "
            f"WHERE ident.id = '{eid}' ORDER BY filter"
        )
        return self._query(adql)

    def get_measurements(
        self,
        identifier: str,
        table: str = "mesVelocities",
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get measurement table entries for an object.

        Args:
            identifier: Object name.
            table: SIMBAD measurement table (e.g. mesVelocities, mesDiameters).
            limit: Maximum results.
        """
        eid = _escape(identifier)
        adql = (
            f"SELECT TOP {limit} m.* FROM {table} AS m "
            f"JOIN ident ON m.oidref = ident.oidref "
            f"WHERE ident.id = '{eid}'"
        )
        return self._query(adql)

    def get_notes(self, identifier: str) -> list[str]:
        """Get object notes from SIMBAD web interface.

        Notes are not available via TAP, so this scrapes the sim-basic page.
        Returns list of note strings (may be empty).
        """
        resp = self._client.get(
            SIM_BASIC_URL,
            params={"Ident": identifier, "submit": "SIMBAD search"},
        )
        if not resp.is_success:
            return []
        text = resp.text
        start = text.find("notes:")
        if start < 0:
            return []
        ul_start = text.find("<UL>", start)
        ul_end = text.find("</UL>", ul_start)
        if ul_start < 0 or ul_end < 0:
            return []
        block = text[ul_start:ul_end]
        items = re.findall(r"<LI>\s*(.*?)\s*</LI>", block, re.DOTALL)
        notes = []
        for item in items:
            item = re.sub(r"<!--.*?-->", "", item, flags=re.DOTALL)
            item = re.sub(r"<A[^>]*>(.*?)</A>", r"\1", item)
            item = re.sub(r"<[^>]+>", "", item)
            item = item.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            item = " ".join(item.split()).strip()
            if item:
                notes.append(item)
        return notes

    def query_adql(self, adql: str) -> list[dict[str, Any]]:
        """Execute a raw ADQL query."""
        return self._query(adql)
