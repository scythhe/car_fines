#!/usr/bin/env python3
"""
Daily fleet fine tracker for police.ge.

Reads the tracked plate roster from a CSV, checks every active plate against
police.ge, and maintains a persistent ledger CSV of every fine ever seen
(keyed by receipt number, which police.ge never reuses).

Day 1 (ledger doesn't exist yet): writes one ledger row per fine currently on
record for every tracked plate.

Day 2+: re-checks every plate, diffs against the existing ledger by
receipt_no, appends any fine that wasn't there before, and (with --notify)
sends a single Telegram message listing just the new fines. Plates with no
change produce no output and no message.

Usage:
    python daily_fines_check.py
    python daily_fines_check.py --notify
    python daily_fines_check.py --plates plates.csv --ledger fines_ledger.csv --notify

Env (only read when --notify is passed):
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
    HEADERS,
    ScrapeIntegrityError,
    get_csrf_token,
    normalise,
    search_plate,
)

LEDGER_FIELDS = [
    "plate", "receipt_no", "fine_date", "violation_date", "delivered_date",
    "due_date", "amount_gel", "days_left", "article", "location",
    "published_date", "first_seen_at", "last_checked_at",
]

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


# --- roster / ledger I/O -------------------------------------------------------


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
        return {row["receipt_no"]: row for row in reader if row.get("receipt_no")}


def write_ledger(path: Path, ledger: dict[str, dict]) -> None:
    rows = sorted(ledger.values(), key=lambda r: (r["plate"], r["fine_date"]))
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# --- notification ---------------------------------------------------------------


def format_message(new_rows: list[dict]) -> str:
    lines = [f"\U0001F6A8 {len(new_rows)} ახალი ჯარიმა შემოწმებულ ავტოპარკზე:", ""]
    for r in new_rows:
        lines.append(
            f"\U0001F697 {r['plate']}\n"
            f"თანხა: {r['amount_gel']:.0f} ₾\n"
            f"მუხლი: {r['article']}\n"
            f"დარჩენილია: {r['days_left']} დღე\n"
            f"ქვითარი: {r['receipt_no']}\n"
        )
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    try:
        resp = requests.post(
            TELEGRAM_API.format(token=token),
            data={"chat_id": chat_id, "text": text},
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
        return bool(payload.get("ok"))
    except Exception as e:
        print(f"[telegram] notify failed, data is still in the ledger csv: {e}",
              file=sys.stderr)
        return False


# --- main run ---------------------------------------------------------------


def run(plates_path: Path, ledger_path: Path, delay: float, notify: bool,
        verbose: bool = False) -> int:
    plates = load_roster(plates_path)
    if not plates:
        print(f"no active plates in {plates_path}", file=sys.stderr)
        return 1

    ledger = load_ledger(ledger_path)
    is_first_run = not ledger_path.exists()
    checked_at = time.strftime("%Y-%m-%d %H:%M:%S")

    session = requests.Session()
    csrf_token = get_csrf_token(session, verbose)

    new_rows: list[dict] = []

    for i, plate in enumerate(plates):
        status, raw_rows = "error", []
        for attempt in (1, 2):
            try:
                raw_rows, status = search_plate(session, plate, csrf_token, verbose)
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
                    csrf_token = get_csrf_token(session, verbose)
                except Exception:
                    pass

        if status == "ok":
            for raw in raw_rows:
                row = normalise(raw, plate, checked_at)
                existing = ledger.get(row["receipt_no"])
                if existing:
                    existing["last_checked_at"] = checked_at
                else:
                    row["first_seen_at"] = checked_at
                    row["last_checked_at"] = checked_at
                    ledger[row["receipt_no"]] = row
                    new_rows.append(row)
            print(f"[{plate}] {len(raw_rows)} fine(s) on record", file=sys.stderr)
        elif status == "empty":
            print(f"[{plate}] no fines", file=sys.stderr)
        else:
            print(f"[{plate}] {status}", file=sys.stderr)

        if i < len(plates) - 1 and delay:
            time.sleep(delay)

    write_ledger(ledger_path, ledger)

    if is_first_run:
        print(f"initialised ledger with {len(new_rows)} fine(s) across "
              f"{len(plates)} plate(s)", file=sys.stderr)
        return 0

    if not new_rows:
        print("no new fines", file=sys.stderr)
        return 0

    print(f"{len(new_rows)} new fine(s) since last check", file=sys.stderr)

    if notify:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            print("[telegram] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set, "
                  "skipping notify (fines are still in the ledger csv)",
                  file=sys.stderr)
        else:
            send_telegram(token, chat_id, format_message(new_rows))

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Daily police.ge fine check for a tracked plate roster.")
    ap.add_argument("--plates", default="plates.csv",
                    help="roster CSV with a 'plate' column (default: plates.csv)")
    ap.add_argument("--ledger", default="fines_ledger.csv",
                    help="persistent fines ledger CSV (default: fines_ledger.csv)")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="seconds between plates (default: 1.5)")
    ap.add_argument("--notify", action="store_true",
                    help="send new fines to Telegram (needs TELEGRAM_BOT_TOKEN "
                         "and TELEGRAM_CHAT_ID env vars); silent if none are new")
    ap.add_argument("--headful", action="store_true",
                    help="verbose: log the underlying HTTP requests/responses")
    args = ap.parse_args()

    return run(Path(args.plates), Path(args.ledger), args.delay, args.notify,
               verbose=args.headful)


if __name__ == "__main__":
    sys.exit(main())
