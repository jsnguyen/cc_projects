#!/usr/bin/env python3
"""CLI tool to search the SIMBAD astronomical database.

Uses the TAP/ADQL endpoint at CDS Strasbourg. No API key needed.
"""

import argparse
import json
import sys
import textwrap

from simbad import SimbadClient, SimbadError

# Redshift threshold — below this absolute value, treat as negligible
# (nearby stars with tiny relativistic corrections)
REDSHIFT_THRESHOLD = 0.001


# ── Formatting helpers ────────────────────────────────────────


def _ra_to_hms(ra: float) -> str:
    """Convert RA in degrees to HH MM SS.ss format."""
    h = ra / 15.0
    hours = int(h)
    m = (h - hours) * 60
    minutes = int(m)
    seconds = (m - minutes) * 60
    return f"{hours:02d}h {minutes:02d}m {seconds:05.2f}s"


def _dec_to_dms(dec: float) -> str:
    """Convert Dec in degrees to +DD MM SS.s format."""
    sign = "+" if dec >= 0 else "-"
    dec = abs(dec)
    degrees = int(dec)
    m = (dec - degrees) * 60
    minutes = int(m)
    seconds = (m - minutes) * 60
    return f"{sign}{degrees:02d}d {minutes:02d}m {seconds:04.1f}s"


def _fmt_coord(ra, dec) -> str:
    if ra is None or dec is None:
        return "-- --"
    return f"{ra:.4f} {dec:+.4f}"


def _fmt_flux_table(fluxes: list[dict], indent: int = 20) -> str:
    """Format fluxes as a small aligned table under the Magnitudes label."""
    if not fluxes:
        return ""
    # Filter to entries that have a value
    entries = [(f.get("filter", "?"), f.get("flux"), f.get("flux_err"))
               for f in fluxes if f.get("flux") is not None]
    if not entries:
        return ""
    pad = " " * indent
    # Header
    lines = [f"{'Band':>4}    {'Mag':>7}    {'Err':>7}"]
    lines.append(f"{'─' * 4}    {'─' * 7}    {'─' * 7}")
    for band, val, err in entries:
        err_str = f"{err:>7.3f}" if err is not None else f"{'—':>7}"
        lines.append(f"{band:>4}    {val:>7.2f}    {err_str}")
    return ("\n" + pad).join(lines)


def _simbad_url(main_id: str) -> str:
    encoded = main_id.replace("+", "%2B").replace(" ", "+")
    return f"https://simbad.cds.unistra.fr/simbad/sim-basic?Ident={encoded}"


# ── Display functions ─────────────────────────────────────────


def _row(label: str, value: str, w: int = 18) -> str:
    """Format a label-value row with aligned columns."""
    return f"  {label:<{w}}{value}"


def display(objects: list[dict], fluxes: list[dict] | None = None,
            notes: list[str] | None = None):
    W = 18  # label column width

    for obj in objects:
        name = obj.get("main_id", "?")
        otype = obj.get("otype_txt") or obj.get("otype", "")
        ra = obj.get("ra")
        dec = obj.get("dec")

        print(f"{name} ({otype})")
        if ra is not None and dec is not None:
            print(_row("ICRS", f"{ra:.6f}  {dec:+.6f}", W))
            print(_row("", f"{_ra_to_hms(ra)}  {_dec_to_dms(dec)}", W))

        sp = obj.get("sp_type")
        if sp:
            print(_row("Spectral type", sp, W))

        morph = obj.get("morph_type")
        if morph:
            print(_row("Morphology", morph, W))

        plx = obj.get("plx_value")
        plx_err = obj.get("plx_err")
        if plx is not None:
            err_str = f" +/- {plx_err}" if plx_err else ""
            print(_row("Parallax [mas]", f"{plx}{err_str}", W))

        pmra = obj.get("pmra")
        pmdec = obj.get("pmdec")
        if pmra is not None or pmdec is not None:
            ra_err = obj.get("pm_err_maj")
            dec_err = obj.get("pm_err_min")
            ra_s = f"{pmra:.2f}" + (f" +/- {ra_err:.2f}" if ra_err else "") if pmra is not None else ""
            dec_s = f"{pmdec:.2f}" + (f" +/- {dec_err:.2f}" if dec_err else "") if pmdec is not None else ""
            print(_row("PM [mas/yr]", f"{ra_s}, {dec_s}", W))

        rv = obj.get("rvz_radvel")
        if rv is not None:
            rv_err = obj.get("rvz_err")
            err_str = f" +/- {rv_err}" if rv_err else ""
            print(_row("Radial vel [km/s]", f"{rv:.1f}{err_str}", W))

        z = obj.get("rvz_redshift")
        if z is not None and abs(z) >= REDSHIFT_THRESHOLD:
            print(_row("Redshift", str(z), W))

        maj = obj.get("galdim_majaxis")
        minor = obj.get("galdim_minaxis")
        if maj is not None:
            dim = f"{maj}"
            if minor is not None:
                dim += f" x {minor}"
            print(_row("Angular size [']", dim, W))

        if fluxes:
            flux_table = _fmt_flux_table(fluxes, indent=2 + W)
            if flux_table:
                print(_row("Magnitudes", flux_table, W))

        print(_row("SIMBAD", _simbad_url(name), W))

        if notes:
            indent = " " * (2 + W)
            for i, note in enumerate(notes):
                label = "Notes" if i == 0 else ""
                wrapped = textwrap.wrap(note, width=80 - 2 - W)
                for j, line in enumerate(wrapped):
                    if j == 0:
                        print(_row(label, line, W))
                    else:
                        print(f"{indent}{line}")

        print()


def display_brief(objects: list[dict]):
    for obj in objects:
        name = obj.get("main_id", "?")
        otype = obj.get("otype_txt") or obj.get("otype", "")
        ra = obj.get("ra")
        dec = obj.get("dec")
        rv = obj.get("rvz_radvel")

        parts = [name, otype, _fmt_coord(ra, dec)]
        if rv is not None:
            parts.append(f"rv={rv:.0f} km/s")
        print(" | ".join(parts))


def display_json(data):
    print(json.dumps(data, indent=2, default=str))


def display_ids(identifiers: list[str], name: str):
    print(f"Identifiers for {name} ({len(identifiers)} total):\n")
    for ident in identifiers:
        print(f"  {ident}")
    print()


# ── Main ──────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Search the SIMBAD astronomical database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              %(prog)s M31                          # identifier lookup
              %(prog)s "NGC 1275" --json            # JSON output
              %(prog)s M31 --ids                    # all known aliases
              %(prog)s --cone 10.68 41.27 0.5       # cone search
              %(prog)s --otype Psr -n 20            # find pulsars
              %(prog)s --adql "SELECT TOP 5 ..."    # raw ADQL
        """),
    )

    parser.add_argument("identifier", nargs="?", default=None, help="Object name (e.g. M31, Vega, 'NGC 1275')")
    parser.add_argument("--cone", nargs=3, type=float, metavar=("RA", "DEC", "RADIUS"), help="Cone search (RA Dec radius in degrees)")
    parser.add_argument("--otype", default=None, help="Filter by object type (e.g. Psr, QSO, Y*O)")
    parser.add_argument("--sp-type", default=None, help="Filter by spectral type (SQL LIKE pattern, e.g. 'O%%')")
    parser.add_argument("--adql", default=None, help="Raw ADQL query")
    parser.add_argument("--ids", action="store_true", help="List all known identifiers")
    parser.add_argument("-n", "--num", type=int, default=20, help="Max results (default: 20)")
    parser.add_argument("-b", "--brief", action="store_true", help="Brief single-line output")
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    if not args.identifier and not args.cone and not args.otype and not args.sp_type and not args.adql:
        parser.print_help()
        sys.exit(1)

    try:
        with SimbadClient() as client:
            if args.adql:
                results = client.query_adql(args.adql)
                if args.json:
                    display_json(results)
                elif args.brief:
                    display_brief(results)
                else:
                    display(results)

            elif args.cone:
                ra, dec, radius = args.cone
                results = client.query_cone(
                    ra, dec, radius, limit=args.num, otype=args.otype
                )
                if args.json:
                    display_json(results)
                elif args.brief:
                    display_brief(results)
                else:
                    display(results)

            elif args.identifier and args.ids:
                ids = client.get_identifiers(args.identifier)
                if not ids:
                    print(f"Object not found: {args.identifier}", file=sys.stderr)
                    sys.exit(1)
                if args.json:
                    display_json(ids)
                else:
                    display_ids(ids, args.identifier)

            elif args.identifier:
                results = client.query_id(args.identifier)
                if not results:
                    print(f"Object not found: {args.identifier}", file=sys.stderr)
                    sys.exit(1)
                fluxes = client.get_fluxes(args.identifier)
                notes = client.get_notes(args.identifier)
                if args.json:
                    data = results[0]
                    data["fluxes"] = fluxes
                    data["notes"] = notes
                    display_json(data)
                elif args.brief:
                    display_brief(results)
                else:
                    display(results, fluxes=fluxes, notes=notes)

            elif args.otype or args.sp_type:
                kwargs = {"limit": args.num}
                if args.otype:
                    kwargs["otype"] = args.otype
                if args.sp_type:
                    kwargs["sp_type"] = args.sp_type
                results = client.query_criteria(**kwargs)
                if args.json:
                    display_json(results)
                elif args.brief:
                    display_brief(results)
                else:
                    display(results)

            else:
                parser.print_help()
                sys.exit(1)

    except SimbadError as e:
        print(f"SIMBAD error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
