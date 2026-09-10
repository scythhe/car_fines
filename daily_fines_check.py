#!/usr/bin/env python3
"""
Daily fleet fine tracker for police.ge.

Reads the tracked plate roster from a CSV and checks every active plate against
BOTH police.ge fine sites:

    protocol   patrol / speed-camera fines   (receipt prefix "ავ")
    municipal  parking / municipal fines     (receipt prefix "ვჯ")

Maintains a persistent ledger CSV of every fine ever seen (keyed by receipt
number, which police.ge never reuses) and an append-only run log so you can
confirm each check actually happened -- not just "no message, so probably fine".

Day 1 (ledger doesn't exist yet): writes one ledger row per fine currently on
record for every tracked plate, on both sites.

Day 2+: re-checks every plate on both sites, diffs against the existing ledger
by receipt_no, appends any fine that wasn't there before, and (with --notify)
sends a Telegram message listing just the new fines. Plates with no change
produce no alert.

Every run appends one row per site to check_log.csv:
    run_at, source, plates_checked, plates_ok, plates_empty, plates_with_fines,
    plates_error, fines_on_record, new_fines, status
status is ok / partial / failed. This file is the "yes, it ran" evidence.

Usage:
    python daily_fines_check.py
    python daily_fines_check.py --notify
    python daily_fines_check.py --notify --heartbeat
    python daily_fines_check.py --sources protocol            # one site only

Env (only read when --notify / --heartbeat is passed):
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

Install:
    pip install requests
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

import requests

from police_fines import (
    SOURCES,
    ScrapeIntegrityError,
    get_csrf_token,
    normalise,
    search_plate,
)

LEDGER_FIELDS = [
    "source", "plate", "receipt_no", "fine_date", "violation_date",
    "delivered_date", "due_date", "amount_gel", "days_left", "article",
    "location", "published_date", "first_seen_at", "last_checked_at",
]

CHECK_LOG_FIELDS = [
    "run_at", "source", "plates_checked", "plates_ok", "plates_empty",
    "plates_with_fines", "plates_error", "fines_on_record", "new_fines", "status",
]

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

SOURCE_LABEL_KA = {"protocol": "საპატრულო", "municipal": "მუნიციპალური"}


# --- roster / ledger / log I/O ----------------------------------------------


def load_roster(path: Path) -> list[str]:
    plates: list[str] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            plate = (row.get("plate") or "").strip().upper()
            status = (row.get("status") or "active").strip().lower()
            if not plate or plate.startswith("#"):
                continue
            if status in ("cleared", "removed", "inactive"):
                continue
            plates.append(plate)
    return list(dict.fromkeys(plates))  # dedupe, keep order


def load_ledger(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        ledger = {}
        for row in reader:
            if not row.get("receipt_no"):
                continue
            row.setdefault("source", "protocol")  # pre-municipal rows
            ledger[row["receipt_no"]] = row
        return ledger


def write_ledger(path: Path, ledger: dict[str, dict]) -> None:
    rows = sorted(ledger.values(),
                  key=lambda r: (r.get("source", ""), r["plate"], r["fine_date"]))
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def append_check_log(path: Path, entries: list[dict]) -> None:
    exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CHECK_LOG_FIELDS, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerows(entries)


# --- notification ---------------------------------------------------------------


def format_alert(new_rows: list[dict]) -> str:
    lines = [f"\U0001F6A8 {len(new_rows)} ახალი ჯარიმა შემოწმებულ ავტოპარკზე:", ""]
    for r in new_rows:
        amount = r["amount_gel"]
        amount_s = f"{float(amount):.0f}" if amount not in (None, "") else "?"
        src = SOURCE_LABEL_KA.get(r.get("source", ""), r.get("source", ""))
        lines.append(
            f"\U0001F697 {r['plate']}  ({src})\n"
            f"თანხა: {amount_s} ₾\n"
            f"მუხლი: {r['article']}\n"
            f"დარჩენილია: {r['days_left']} დღე\n"
            f"ქვითარი: {r['receipt_no']}\n"
        )
    return "\n".join(lines)


def format_heartbeat(run_at: str, log_entries: list[dict]) -> str:
    parts = [f"✅ შემოწმება დასრულდა ({run_at})"]
    for e in log_entries:
        src = SOURCE_LABEL_KA.get(e["source"], e["source"])
        parts.append(
            f"{src}: {e['plates_ok']}/{e['plates_checked']} ნომერი, "
            f"{e['fines_on_record']} ჯარიმა ბაზაში"
            + (f", შეცდომა: {e['plates_error']}" if e["plates_error"] else "")
            + (f" [{e['status'].upper()}]" if e["status"] != "ok" else "")
        )
    total_new = sum(int(e["new_fines"]) for e in log_entries)
    parts.append("")
    parts.append("ახალი ჯარიმა არ არის." if not total_new
                 else f"ახალი ჯარიმა: {total_new} (იხ. ცალკე შეტყობინება).")
    return "\n".join(parts)


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    try:
        resp = requests.post(
            TELEGRAM_API.format(token=token),
            data={"chat_id": chat_id, "text": text},
            timeout=15,
        )
        resp.raise_for_status()
        return bool(resp.json().get("ok"))
    except Exception as e:
        print(f"[telegram] notify failed, data is still in the ledger csv: {e}",
              file=sys.stderr)
        return False


# --- per-source check ------------------------------------------------------


def check_source(source: str, plates: list[str], ledger: dict[str, dict],
                 checked_at: str, delay: float, verbose: bool) -> tuple[dict, list[dict]]:
    """Check every plate on one site. Returns (log_entry, new_rows)."""
    base_url = SOURCES[source]
    c = {"checked": 0, "ok": 0, "empty": 0, "with_fines": 0, "error": 0,
         "fines_on_record": 0}
    new_rows: list[dict] = []

    session = requests.Session()
    try:
        csrf_token = get_csrf_token(session, base_url, verbose)
    except Exception as e:
        print(f"[{source}] could not open site: {e}", file=sys.stderr)
        c["checked"] = c["error"] = len(plates)
        return _log_entry(source, checked_at, c, 0, "failed"), []

    for i, plate in enumerate(plates):
        c["checked"] += 1
        status, raw_rows = "error", []
        for attempt in (1, 2):
            try:
                raw_rows, status = search_plate(session, plate, csrf_token, base_url, verbose)
                break
            except ScrapeIntegrityError as e:
                status = f"mismatch: {e}"
                break
            except requests.RequestException as e:
                status = f"error: {e}"
            except (ValueError, KeyError) as e:
                status = f"error: {e}"
            if attempt == 1:
                try:
                    csrf_token = get_csrf_token(session, base_url, verbose)
                except Exception:
                    pass

        if status == "ok":
            c["ok"] += 1
            c["with_fines"] += 1
            c["fines_on_record"] += len(raw_rows)
            for raw in raw_rows:
                row = normalise(raw, plate, checked_at, source)
                existing = ledger.get(row["receipt_no"])
                if existing:
                    existing["last_checked_at"] = checked_at
                else:
                    row["first_seen_at"] = checked_at
                    row["last_checked_at"] = checked_at
                    ledger[row["receipt_no"]] = row
                    new_rows.append(row)
            print(f"[{source}:{plate}] {len(raw_rows)} fine(s) on record", file=sys.stderr)
        elif status == "empty":
            c["ok"] += 1
            c["empty"] += 1
            print(f"[{source}:{plate}] no fines", file=sys.stderr)
        else:
            c["error"] += 1
            print(f"[{source}:{plate}] {status}", file=sys.stderr)

        if i < len(plates) - 1 and delay:
            time.sleep(delay)

    if c["error"] == 0:
        run_status = "ok"
    elif c["error"] < c["checked"]:
        run_status = "partial"
    else:
        run_status = "failed"

    return _log_entry(source, checked_at, c, len(new_rows), run_status), new_rows


def _log_entry(source: str, run_at: str, c: dict, new_fines: int, status: str) -> dict:
    return {
        "run_at": run_at,
        "source": source,
        "plates_checked": c["checked"],
        "plates_ok": c["ok"],
        "plates_empty": c["empty"],
        "plates_with_fines": c["with_fines"],
        "plates_error": c["error"],
        "fines_on_record": c["fines_on_record"],
        "new_fines": new_fines,
        "status": status,
    }


# --- main run ---------------------------------------------------------------


def run(plates_path: Path, ledger_path: Path, check_log_path: Path,
        sources: list[str], delay: float, notify: bool, heartbeat: bool,
        verbose: bool = False) -> int:
    plates = load_roster(plates_path)
    if not plates:
        print(f"no active plates in {plates_path}", file=sys.stderr)
        return 1

    ledger = load_ledger(ledger_path)
    is_first_run = not ledger_path.exists()
    run_at = time.strftime("%Y-%m-%d %H:%M:%S")

    log_entries: list[dict] = []
    all_new: list[dict] = []
    for source in sources:
        entry, new_rows = check_source(source, plates, ledger, run_at, delay, verbose)
        log_entries.append(entry)
        all_new.extend(new_rows)

    write_ledger(ledger_path, ledger)
    append_check_log(check_log_path, log_entries)

    # human-readable summary to stderr / CI log
    for e in log_entries:
        print(f"[{e['source']}] checked {e['plates_checked']} plate(s): "
              f"{e['plates_ok']} ok, {e['plates_error']} error, "
              f"{e['fines_on_record']} fine(s) on record, {e['new_fines']} new "
              f"-> {e['status'].upper()}", file=sys.stderr)

    any_failed = any(e["status"] == "failed" for e in log_entries)

    if is_first_run:
        print(f"initialised ledger with {len(all_new)} fine(s) across "
              f"{len(plates)} plate(s) x {len(sources)} site(s)", file=sys.stderr)
    elif all_new:
        print(f"{len(all_new)} new fine(s) since last check", file=sys.stderr)
    else:
        print("no new fines", file=sys.stderr)

    if notify or heartbeat:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            print("[telegram] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set, "
                  "skipping messages (data is still in the ledger + check log)",
                  file=sys.stderr)
        else:
            if notify and all_new and not is_first_run:
                send_telegram(token, chat_id, format_alert(all_new))
            if heartbeat:
                send_telegram(token, chat_id, format_heartbeat(run_at, log_entries))

    return 2 if any_failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Daily police.ge fine check (protocol + municipal) for a "
                    "tracked plate roster.")
    ap.add_argument("--plates", default="plates.csv",
                    help="roster CSV with a 'plate' column (default: plates.csv)")
    ap.add_argument("--ledger", default="fines_ledger.csv",
                    help="persistent fines ledger CSV (default: fines_ledger.csv)")
    ap.add_argument("--check-log", default="check_log.csv",
                    help="append-only run log CSV (default: check_log.csv)")
    ap.add_argument("--sources", default="protocol,municipal",
                    help="comma-separated sites to check: protocol, municipal "
                         "(default: both)")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="seconds between plate requests (default: 1.5)")
    ap.add_argument("--notify", action="store_true",
                    help="Telegram-alert on NEW fines (needs TELEGRAM_BOT_TOKEN "
                         "and TELEGRAM_CHAT_ID); silent when nothing is new")
    ap.add_argument("--heartbeat", action="store_true",
                    help="also send a short Telegram summary every run, so you "
                         "can see the check happened even with no new fines")
    ap.add_argument("--headful", action="store_true",
                    help="verbose: log the underlying HTTP requests/responses")
    args = ap.parse_args()

    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    bad = [s for s in sources if s not in SOURCES]
    if bad:
        ap.error(f"unknown source(s): {bad}; valid: {list(SOURCES)}")

    return run(Path(args.plates), Path(args.ledger), Path(args.check_log),
               sources, args.delay, args.notify, args.heartbeat,
               verbose=args.headful)


if __name__ == "__main__":
    sys.exit(main())
