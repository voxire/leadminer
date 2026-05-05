"""
Push the latest leadminer CSV into the Voxire Sales Google Sheet.

Merge semantics (idempotent, run monthly):
  - Each lead is keyed by phone (preferred) or by (name, region) fallback.
  - Existing rows: refresh the auto-imported fields (lead_score, recommended_service, etc.),
    preserve the helper-managed fields (owner, status, notes, etc.).
  - New leads (not in sheet): append, flag is_new = "YES".
  - Sheet rows missing from new CSV: flag is_stale = "YES" (leave row + notes intact).
  - Bump last_seen_at to today on every match.

Environment variables required:
  SHEETS_ID                    The target Google Sheet ID (from its URL).
  GOOGLE_SERVICE_ACCOUNT_KEY   JSON string of the service account credentials.

Usage (locally, for testing):
  export SHEETS_ID="..."
  export GOOGLE_SERVICE_ACCOUNT_KEY="$(cat /path/to/service-account.json)"
  python scripts/push_to_sheets.py [--csv data/sales_ready.csv] [--tab Leads]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials


# ---- Column groups ----

# Columns the script writes from the CSV. These are overwritten on every run.
AUTO_FIELDS = [
    "name", "category", "region", "country", "address",
    "phone", "email", "website", "website_live",
    "facebook", "instagram", "whatsapp", "linkedin",
    "rating", "review_count", "completeness_score", "lead_score",
    "industry_priority", "recommended_service",
    "source", "scraped_at",
]

# Columns the script manages but doesn't take from the CSV.
STATE_FIELDS = ["last_seen_at", "first_seen_at", "is_new", "is_stale"]

# Columns helpers fill in. The script NEVER overwrites these on existing rows.
HELPER_FIELDS = [
    "owner", "status", "last_touch_date",
    "next_action", "next_action_date", "notes",
]

# Final column order in the sheet (left to right)
ALL_FIELDS = AUTO_FIELDS + STATE_FIELDS + HELPER_FIELDS

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


# ---- Helpers ----

def make_key(record: dict) -> str:
    """Stable identifier for matching a lead across runs."""
    phone = (record.get("phone") or "").strip()
    if phone:
        return f"phone:{phone}"
    name = (record.get("name") or "").strip().lower()
    region = (record.get("region") or "").strip().lower()
    return f"name:{name}|{region}"


def authorize() -> gspread.Client:
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_KEY")
    if not creds_json:
        sys.exit("ERROR: GOOGLE_SERVICE_ACCOUNT_KEY not set.")
    info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def open_or_create_tab(sheet, tab_name: str):
    try:
        ws = sheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        print(f"[push] Tab '{tab_name}' not found, creating...")
        ws = sheet.add_worksheet(title=tab_name, rows=10000, cols=len(ALL_FIELDS))
        ws.update(values=[ALL_FIELDS], range_name="A1")
        return ws, []
    rows = ws.get_all_records() if ws.row_count > 1 else []
    return ws, rows


def merge(new_records: list[dict], existing_rows: list[dict], today: str) -> dict:
    """
    Returns a dict with:
      out_rows: list of dicts, in final write order
      stats: dict of counts
    """
    new_index = {make_key(r): r for r in new_records}
    existing_index = {make_key(r): r for r in existing_rows}

    out_rows: list[dict] = []
    seen_keys: set[str] = set()
    n_updated = n_stale = n_new = 0

    # Pass 1: existing rows in sheet — update or mark stale
    for key, old in existing_index.items():
        if key in new_index:
            new_data = new_index[key]
            merged = dict(old)  # preserve everything
            # Refresh only AUTO fields
            for field in AUTO_FIELDS:
                merged[field] = new_data.get(field, "")
            merged["last_seen_at"] = today
            merged["is_new"] = ""
            merged["is_stale"] = ""
            if not merged.get("first_seen_at"):
                merged["first_seen_at"] = old.get("first_seen_at") or today
            out_rows.append(merged)
            seen_keys.add(key)
            n_updated += 1
        else:
            old["is_stale"] = "YES"
            out_rows.append(old)
            n_stale += 1

    # Pass 2: brand-new leads
    for key, new_data in new_index.items():
        if key in seen_keys:
            continue
        row = {field: new_data.get(field, "") for field in AUTO_FIELDS}
        row["first_seen_at"] = today
        row["last_seen_at"] = today
        row["is_new"] = "YES"
        row["is_stale"] = ""
        for field in HELPER_FIELDS:
            row[field] = ""
        out_rows.append(row)
        n_new += 1

    return {
        "out_rows": out_rows,
        "stats": {"updated": n_updated, "new": n_new, "stale": n_stale},
    }


def write_back(ws, rows: list[dict]) -> None:
    """Single batched write: clear, then upload header + rows."""
    body = [ALL_FIELDS] + [
        [str(r.get(field, "")) for field in ALL_FIELDS] for r in rows
    ]
    ws.clear()
    # gspread auto-batches large updates; passing range_name="A1" anchors top-left
    ws.update(values=body, range_name="A1")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--csv", default="data/sales_ready.csv",
        help="CSV file to push (default: data/sales_ready.csv)",
    )
    parser.add_argument(
        "--tab", default="Leads",
        help="Sheet tab name (default: Leads)",
    )
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        sys.exit(f"ERROR: CSV not found at {csv_path}")

    sheet_id = os.environ.get("SHEETS_ID")
    if not sheet_id:
        sys.exit("ERROR: SHEETS_ID not set.")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with open(csv_path, encoding="utf-8") as f:
        new_records = list(csv.DictReader(f))
    print(f"[push] Loaded {len(new_records)} records from {csv_path.name}")

    gc = authorize()
    sheet = gc.open_by_key(sheet_id)
    ws, existing_rows = open_or_create_tab(sheet, args.tab)
    print(f"[push] Sheet has {len(existing_rows)} existing rows in tab '{args.tab}'")

    result = merge(new_records, existing_rows, today)
    stats = result["stats"]
    print(
        f"[push] Merge: "
        f"{stats['updated']} updated, {stats['new']} new, {stats['stale']} stale"
    )

    write_back(ws, result["out_rows"])
    total_after = len(result["out_rows"])
    print(f"[push] Wrote {total_after} rows to tab '{args.tab}'. Done.")


if __name__ == "__main__":
    main()
