#!/usr/bin/env python3
"""
police.ge fine lookup by license plate.

Two sister sites with an identical API are supported:

    protocol   https://police.ge/protocol/index.php     (patrol / camera fines)
    municipal  https://police.ge/municipal/index.php     (parking / municipal fines)

Both talk to the same JSON endpoint their own JS calls (searchCarForm() in
resources/assets/js/app.js):

    GET  <base>/index.php                       (cookies + csrf_token)
    POST <base>/index.php?url=protocols/searchByAuto
         firstResult=0&protocolAuto=<PLATE>&csrf_token=<token>
    -> {"success": true, "data": {"count": N, "results": [...]}}

No browser automation is involved, so there is no risk of the page's default
"most recent fines" table being mistaken for search results.

Usage:
    python police_fines.py 8814NN
    python police_fines.py 8814NN --source municipal
    python police_fines.py 8814NN --source both
    python police_fines.py --file plates.txt --state seen.json --new-only

Install:
    pip install requests
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

import requests

SOURCES = {
    "protocol": "https://police.ge/protocol/index.php",
    "municipal": "https://police.ge/municipal/index.php",
}
DEFAULT_SOURCE = "protocol"

CSRF_RE = re.compile(r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']')

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def headers_for(base_url: str) -> dict:
    return {"User-Agent": _UA, "Referer": base_url}


# Back-compat: some callers still import HEADERS directly.
HEADERS = headers_for(SOURCES[DEFAULT_SOURCE])

CSV_FIELDS = [
    "source", "plate", "receipt_no", "fine_date", "violation_date",
    "delivered_date", "due_date", "amount_gel", "days_left", "article",
    "location", "published_date", "checked_at",
]

# --- parsing ------------------------------------------------------------------


def fmt_date(iso: str | None) -> str:
    """'2026-08-25' -> '25.08.2026' (matches the site's own display format)."""
    if not iso:
        return ""
    parts = iso.split("-")
    if len(parts) != 3:
        return iso
    y, m, d = parts
    return f"{d}.{m}.{y}"


def to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def to_int(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def normalise(raw: dict, plate_query: str, checked_at: str,
              source: str = DEFAULT_SOURCE) -> dict:
    return {
        "source": source,
        "plate_query": plate_query,
        "plate": raw.get("protocolAuto") or plate_query,
        "receipt_no": raw.get("protocolNo", ""),
        "fine_date": fmt_date(raw.get("protocolDate")),
        "violation_date": fmt_date(raw.get("violationDate")),
        "delivered_date": fmt_date(raw.get("activeDate")),
        "due_date": fmt_date(raw.get("lastDate")),
        "amount_gel": to_float(raw.get("protocolAmount")),
        "days_left": to_int(raw.get("remainingDays")),
        "article": raw.get("protocolLawDescription", ""),
        "location": raw.get("protocolPlace", ""),
        "published_date": fmt_date(raw.get("publishDate")),
        "checked_at": checked_at,
    }


# --- scraping -----------------------------------------------------------------


class ScrapeIntegrityError(Exception):
    """Raised when the server returns rows for a plate we didn't ask about."""


def get_csrf_token(session: requests.Session, base_url: str = SOURCES[DEFAULT_SOURCE],
                   verbose: bool = False) -> str:
    resp = session.get(base_url, headers=headers_for(base_url), timeout=30)
    resp.raise_for_status()
    m = CSRF_RE.search(resp.text)
    if not m:
        raise RuntimeError(f"could not find csrf_token on index page ({base_url})")
    if verbose:
        print(f"[csrf] {m.group(1)}", file=sys.stderr)
    return m.group(1)


def search_plate(
    session: requests.Session, plate: str, csrf_token: str,
    base_url: str = SOURCES[DEFAULT_SOURCE], verbose: bool = False,
) -> tuple[list[dict], str]:
    """Returns (raw_results, status). status in {ok, empty, error, mismatch}."""
    search_url = f"{base_url}?url=protocols/searchByAuto"
    data = {"firstResult": 0, "protocolAuto": plate, "csrf_token": csrf_token}
    if verbose:
        print(f"[req] POST {search_url} protocolAuto={plate}", file=sys.stderr)
    resp = session.post(search_url, data=data, headers=headers_for(base_url), timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if verbose:
        print(f"[resp] {json.dumps(payload, ensure_ascii=False)}", file=sys.stderr)

    if not payload.get("success"):
        return [], f"error: {payload.get('message') or 'unsuccessful response'}"

    results = payload.get("data", {}).get("results") or []
    if not results:
        return [], "empty"

    # Hard integrity check: every returned row must be for the plate we asked
    # about. A mismatch here means we've been handed someone else's fines
    # (e.g. a stale/default table) and must NOT be silently trusted.
    bad = [r for r in results if (r.get("protocolAuto") or "").strip().upper() != plate.upper()]
    if bad:
        raise ScrapeIntegrityError(
            f"plate mismatch for query {plate!r}: got plates "
            f"{sorted({r.get('protocolAuto') for r in bad})}"
        )

    return results, "ok"


def run(plates: list[str], delay: float, source: str = DEFAULT_SOURCE,
        verbose: bool = False) -> list[dict]:
    results: list[dict] = []
    checked_at = time.strftime("%Y-%m-%d %H:%M:%S")
    base_url = SOURCES[source]

    session = requests.Session()
    csrf_token = get_csrf_token(session, base_url, verbose)

    for i, plate in enumerate(plates):
        status, raw_rows = "error", []
        for attempt in (1, 2):
            try:
                raw_rows, status = search_plate(session, plate, csrf_token, base_url, verbose)
                break
            except ScrapeIntegrityError as e:
                status = f"mismatch: {e}"
                break  # never retry a mismatch into looking like success
            except requests.RequestException as e:
                status = f"error: {e}"
            except (ValueError, KeyError) as e:  # bad/non-JSON response
                status = f"error: {e}"
            if attempt == 1:
                try:
                    csrf_token = get_csrf_token(session, base_url, verbose)
                except Exception:
                    pass

        if status == "ok":
            found = [normalise(r, plate, checked_at, source) for r in raw_rows]
            results.extend(found)
            print(f"[{source}:{plate}] {len(found)} fine(s)", file=sys.stderr)
        else:
            print(f"[{source}:{plate}] {status}", file=sys.stderr)

        if i < len(plates) - 1 and delay:
            time.sleep(delay)

    return results


# --- state (new-fine detection) ----------------------------------------------


def load_state(path: Path) -> set[str]:
    if path and path.exists():
        return set(json.loads(path.read_text(encoding="utf-8")))
    return set()


def save_state(path: Path, keys: set[str]) -> None:
    path.write_text(json.dumps(sorted(keys), ensure_ascii=False, indent=1),
                    encoding="utf-8")


def key_of(row: dict) -> str:
    return row["receipt_no"] or f'{row["plate"]}|{row["fine_date"]}|{row["amount_gel"]}'


# --- main ---------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Check police.ge fines by plate number.")
    ap.add_argument("plates", nargs="*", help="plate numbers, e.g. 8814NN AA963AA")
    ap.add_argument("--file", help="text file with one plate per line")
    ap.add_argument("--source", choices=[*SOURCES, "both"], default=DEFAULT_SOURCE,
                    help="which site(s) to query (default: protocol)")
    ap.add_argument("--csv", help="write results to CSV")
    ap.add_argument("--json", help="write results to JSON")
    ap.add_argument("--state", help="JSON file of already-seen receipts")
    ap.add_argument("--new-only", action="store_true",
                    help="with --state: only output fines not seen before")
    ap.add_argument("--headful", action="store_true",
                    help="verbose: log the underlying HTTP requests/responses "
                         "(no browser is used, this endpoint is called directly)")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between plates")
    args = ap.parse_args()

    plates = [p.strip().upper().replace(" ", "") for p in args.plates]
    if args.file:
        plates += [
            ln.strip().upper().replace(" ", "")
            for ln in Path(args.file).read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
    plates = list(dict.fromkeys(plates))  # dedupe, keep order
    if not plates:
        ap.error("no plates given (positional args or --file)")

    sources = list(SOURCES) if args.source == "both" else [args.source]
    rows: list[dict] = []
    for src in sources:
        rows.extend(run(plates, args.delay, src, verbose=args.headful))

    seen: set[str] = set()
    if args.state:
        state_path = Path(args.state)
        seen = load_state(state_path)
        fresh = [r for r in rows if key_of(r) not in seen]
        save_state(state_path, seen | {key_of(r) for r in rows})
        print(f"{len(fresh)} new fine(s)", file=sys.stderr)
        if args.new_only:
            rows = fresh

    if args.json:
        Path(args.json).write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    if not args.json and not args.csv:
        print(json.dumps(rows, ensure_ascii=False, indent=2))

    total = sum(r["amount_gel"] or 0 for r in rows)
    print(f"total: {len(rows)} fine(s), {total:.0f} GEL", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
