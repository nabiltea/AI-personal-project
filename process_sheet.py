"""
Process inbound wholesale inquiries straight from the Google Forms responses sheet.

Google Forms writes every submission into a linked spreadsheet. This script
reads that sheet, finds rows that have not been processed yet, runs each one
through the same Gemini enrichment and HubSpot write used by enrich_lead.py,
then marks the row as processed so it is not handled twice.

Run:
    python process_sheet.py
    python process_sheet.py --dry-run      # read and match, but no writes
    python process_sheet.py --limit 5      # process at most 5 rows
"""

import argparse
import os
import sys

from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from enrich_lead import (
    associate_deal_with_contact,
    create_deal,
    enrich_with_gemini,
    upsert_contact,
)

load_dotenv()

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SERVICE_ACCOUNT_FILE = os.getenv(
    "GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"
)
SHEET_NAME = os.getenv("SHEET_NAME", "Form Responses 1")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Column header on the responses sheet used to record that a row is done.
# Add this header yourself in the first empty column; Forms will not touch it.
PROCESSED_HEADER = "Processed"

# Maps the fields the pipeline needs to the words that appear in the sheet's
# header row. Matching on header text rather than column position means the
# script keeps working if the form's questions are reordered, which happens.
FIELD_MATCHERS = {
    "first_name": ["first name"],
    "last_name": ["last name"],
    "company_name": ["company name"],
    "company_email": ["company e-mail", "company email", "email"],
    "product_request": ["product request"],
}


def resolve_columns(headers: list) -> dict:
    """Work out which column index holds each field we need.

    Raises if a required field cannot be found, because silently processing
    rows with missing data would write junk into the CRM.
    """
    normalised = [h.strip().lower() for h in headers]
    resolved = {}

    for field, candidates in FIELD_MATCHERS.items():
        index = None
        for candidate in candidates:
            for position, header in enumerate(normalised):
                if candidate in header:
                    index = position
                    break
            if index is not None:
                break

        if index is None:
            raise RuntimeError(
                f"Could not find a column for '{field}' in the sheet headers: "
                f"{headers}"
            )
        resolved[field] = index

    processed_index = next(
        (
            position
            for position, header in enumerate(normalised)
            if header == PROCESSED_HEADER.lower()
        ),
        None,
    )
    if processed_index is None:
        raise RuntimeError(
            f"Add a '{PROCESSED_HEADER}' column header to the sheet so the "
            "script can record which rows it has already handled."
        )
    resolved["_processed"] = processed_index
    return resolved


def cell(row: list, index: int) -> str:
    """Google omits trailing empty cells, so index directly and you get errors."""
    return row[index].strip() if index < len(row) else ""


def row_to_submission(row: list, columns: dict) -> dict:
    return {
        "first_name": cell(row, columns["first_name"]),
        "last_name": cell(row, columns["last_name"]),
        "company_name": cell(row, columns["company_name"]),
        "company_email": cell(row, columns["company_email"]),
        "product_request": cell(row, columns["product_request"]),
    }


def is_processed(row: list, columns: dict) -> bool:
    return bool(cell(row, columns["_processed"]))


def submission_is_complete(submission: dict) -> bool:
    return all(submission.values())


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def get_sheets_service():
    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID is not set. See .env.example.")
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise RuntimeError(
            f"Service account file not found at '{SERVICE_ACCOUNT_FILE}'. "
            "Download it from Google Cloud and place it in the project folder."
        )
    credentials = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=credentials)


def read_rows(service) -> list:
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SPREADSHEET_ID, range=SHEET_NAME)
        .execute()
    )
    return result.get("values", [])


def mark_processed(service, columns: dict, sheet_row_number: int, note: str):
    target = f"{SHEET_NAME}!{column_letter(columns['_processed'])}{sheet_row_number}"
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=target,
        valueInputOption="RAW",
        body={"values": [[note]]},
    ).execute()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which rows would be processed without calling any API",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most this many rows",
    )
    args = parser.parse_args()

    service = get_sheets_service()
    rows = read_rows(service)

    if not rows:
        print("Sheet is empty.")
        return 0

    headers, data_rows = rows[0], rows[1:]
    columns = resolve_columns(headers)

    pending = [
        (position, row)
        for position, row in enumerate(data_rows)
        if not is_processed(row, columns)
    ]

    if not pending:
        print("No new submissions. Everything is already processed.")
        return 0

    if args.limit:
        pending = pending[: args.limit]

    print(f"Found {len(pending)} unprocessed submission(s).\n")

    processed_count = 0
    for position, row in pending:
        sheet_row_number = position + 2  # +1 for header, +1 for 1-based rows
        submission = row_to_submission(row, columns)
        label = submission["company_name"] or f"row {sheet_row_number}"

        if not submission_is_complete(submission):
            print(f"Row {sheet_row_number} ({label}): skipped, missing fields.")
            continue

        if args.dry_run:
            print(f"Row {sheet_row_number} ({label}): would process.")
            continue

        print(f"Row {sheet_row_number} ({label}): processing...")
        try:
            enrichment = enrich_with_gemini(submission["product_request"])
            contact_id = upsert_contact(submission, enrichment)
            deal_id = create_deal(submission, enrichment)
            associate_deal_with_contact(deal_id, contact_id)
        except Exception as error:
            # One bad submission should not stop the rest of the batch. The row
            # stays unmarked, so the next run retries it.
            print(f"  failed: {error}")
            continue

        mark_processed(service, columns, sheet_row_number, f"deal:{deal_id}")
        print(
            f"  done. {enrichment['category']} / urgency "
            f"{enrichment['urgency']} -> deal {deal_id}"
        )
        processed_count += 1

    if args.dry_run:
        print("\nDry run - nothing was written.")
    else:
        print(f"\nProcessed {processed_count} submission(s).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"\nError: {error}", file=sys.stderr)
        sys.exit(1)
