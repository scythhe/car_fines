#!/usr/bin/env python3
"""
Extract Georgian customs transit numbers (6-char: 4 digits + 2 letters, e.g., 6856NN)
from PDF customs declarations (შეტყობინება).

Supports both local files and Google Drive streaming (no download).

Usage — LOCAL FILES:
    python3 extract_transit_number.py *.pdf
    python3 extract_transit_number.py ~/customs_pdfs/*.pdf --csv plates.csv

Usage — GOOGLE DRIVE (streams without downloading):
    python3 extract_transit_number.py --drive-folder FOLDER_ID --csv plates.csv

Install:
    pip install pymupdf google-auth-oauthlib google-auth-httplib2 google-api-python-client

Note: pdfplumber was tried first but silently drops text on some of these customs
PDFs (broken/embedded font -> garbled "(cid:####)" glyph codes instead of real
characters, on ~75% of a real sample). PyMuPDF parses the same fonts' ToUnicode
maps correctly, so it's used for all text extraction here -- plain text, no OCR.

Google Drive setup (one-time):
    1. Go to https://console.cloud.google.com/
    2. Create a new project
    3. Enable "Google Drive API"
    4. Create OAuth 2.0 credentials (Desktop app type)
    5. Download credentials.json → put it in ~/fines/
    6. Run script once → browser opens → authorize → done
    7. token.json is saved for future runs
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from pathlib import Path

try:
    import pymupdf
except ImportError:
    print("Error: pymupdf not installed. Run: pip install pymupdf", file=sys.stderr)
    sys.exit(1)

# Google Drive imports (optional)
try:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    HAS_DRIVE = True
except ImportError:
    HAS_DRIVE = False

SCOPES = ['https://www.googleapis.com/auth/drive.readonly']
TRANSIT_PATTERN = re.compile(r'\b(\d{4}[A-Z]{2})\b')


# --- Google Drive -------------------------------------------------------

def get_drive_creds(creds_file: Path = Path("credentials.json")) -> Credentials:
    """
    Get Google Drive credentials.
    - First run: opens browser for OAuth login, saves token.json
    - Later runs: uses cached token.json
    """
    token_file = Path("token.json")
    creds = None

    # Load cached token if it exists
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    # If no valid token, do OAuth flow
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_file.exists():
                raise FileNotFoundError(
                    f"credentials.json not found in {Path.cwd()}.\n"
                    "Download it from Google Cloud Console:\n"
                    "  1. https://console.cloud.google.com/\n"
                    "  2. Enable Google Drive API\n"
                    "  3. Create OAuth 2.0 credentials (Desktop app)\n"
                    "  4. Download and save as credentials.json here"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
            creds = flow.run_local_server(port=0)

        # Save token for next time
        token_file.write_text(creds.to_json())
        if sys.stderr.isatty():
            print(f"[oauth] credentials saved to token.json", file=sys.stderr)

    return creds


def list_drive_pdfs(folder_id: str) -> list[tuple[str, io.BytesIO]]:
    """
    Stream PDFs from a Google Drive folder into memory (no disk download).
    Returns list of (filename, BytesIO stream) tuples.
    """
    try:
        creds = get_drive_creds()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    service = build('drive', 'v3', credentials=creds)
    
    # Query for PDFs in this folder, excluding trash
    query = f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false"
    
    try:
        results = service.files().list(
            q=query,
            spaces='drive',
            pageSize=100,
            fields='files(id, name)'
        ).execute()
    except Exception as e:
        print(f"Error listing Drive folder: {e}", file=sys.stderr)
        print(f"Make sure folder ID is correct: {folder_id}", file=sys.stderr)
        sys.exit(1)

    files = results.get('files', [])
    if not files:
        print(f"No PDFs found in folder {folder_id}", file=sys.stderr)
        return []

    print(f"[drive] found {len(files)} PDF(s)", file=sys.stderr)
    pdfs = []

    for file in files:
        try:
            # Stream the file into memory
            request = service.files().get_media(fileId=file['id'])
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            
            done = False
            while not done:
                status, done = downloader.next_chunk()
            
            fh.seek(0)  # Reset to beginning for reading
            pdfs.append((file['name'], fh))
            print(f"[drive] {file['name']}", file=sys.stderr)
        except Exception as e:
            print(f"[drive] {file['name']} error: {e}", file=sys.stderr)

    return pdfs


# --- PDF extraction -------------------------------------------------------

def _find_transit_number(text: str, label: str, verbose: bool = False) -> str | None:
    if not text:
        if verbose:
            print(f"[{label}] no text", file=sys.stderr)
        return None

    # Search in the header (first 1000 chars) for the transit number.
    matches = TRANSIT_PATTERN.findall(text[:1000])
    if matches:
        if verbose:
            print(f"[{label}] {matches[0]}", file=sys.stderr)
        return matches[0]

    if verbose:
        print(f"[{label}] not found", file=sys.stderr)
    return None


def extract_transit_number(pdf_path: Path, verbose: bool = False) -> str | None:
    """
    Extract transit number from a local PDF file.
    Returns the first 6-char transit number (4 digits + 2 letters).
    """
    if not pdf_path.exists():
        raise FileNotFoundError(f"{pdf_path}")
    if not pdf_path.suffix.lower() == '.pdf':
        raise ValueError(f"{pdf_path} is not a PDF")

    try:
        with pymupdf.open(pdf_path) as doc:
            if doc.page_count == 0:
                if verbose:
                    print(f"[{pdf_path.name}] no pages", file=sys.stderr)
                return None
            text = doc[0].get_text()
            return _find_transit_number(text, pdf_path.name, verbose)

    except Exception as e:
        print(f"[{pdf_path.name}] error: {e}", file=sys.stderr)
        return None


def extract_transit_number_from_bytes(
    pdf_bytes: io.BytesIO, name: str, verbose: bool = False
) -> str | None:
    """
    Extract transit number from an in-memory PDF (BytesIO).
    Used for Google Drive streams.
    """
    try:
        with pymupdf.open(stream=pdf_bytes.getvalue(), filetype="pdf") as doc:
            if doc.page_count == 0:
                if verbose:
                    print(f"[{name}] no pages", file=sys.stderr)
                return None
            text = doc[0].get_text()
            return _find_transit_number(text, name, verbose)

    except Exception as e:
        print(f"[{name}] error: {e}", file=sys.stderr)
        return None


# --- Main -------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Extract Georgian customs transit numbers from PDFs (local or Google Drive)."
    )
    ap.add_argument("files", nargs="*", help="local PDF files or glob patterns")
    ap.add_argument("--drive-folder", help="Google Drive folder ID to scan")
    ap.add_argument("--csv", help="write results to CSV")
    ap.add_argument("--verbose", action="store_true", help="log details per file")
    args = ap.parse_args()

    results = []

    # --- LOCAL FILES ---
    if args.files:
        pdf_paths: list[Path] = []
        for pattern in args.files:
            p = Path(pattern)
            if p.is_file():
                pdf_paths.append(p)
            else:
                pdf_paths.extend(Path(".").glob(pattern))

        pdf_paths = sorted(set(pdf_paths))

        if not pdf_paths and not args.drive_folder:
            ap.error("no local PDF files found")

        for pdf_path in pdf_paths:
            transit = extract_transit_number(pdf_path, verbose=args.verbose)
            results.append({"file": pdf_path.name, "transit_number": transit or ""})

    # --- GOOGLE DRIVE ---
    if args.drive_folder:
        if not HAS_DRIVE:
            print(
                "Error: Google Drive support requires:\n"
                "  pip install google-auth-oauthlib google-auth-httplib2 google-api-python-client",
                file=sys.stderr,
            )
            return 1

        pdfs = list_drive_pdfs(args.drive_folder)
        for name, pdf_bytes in pdfs:
            transit = extract_transit_number_from_bytes(pdf_bytes, name, verbose=args.verbose)
            results.append({"file": name, "transit_number": transit or ""})

    if not results:
        ap.error("no PDFs processed")

    # --- OUTPUT ---
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["file", "transit_number"])
            w.writeheader()
            w.writerows(results)
        found = sum(1 for r in results if r["transit_number"])
        print(f"wrote {len(results)} results to {args.csv} ({found} found)", file=sys.stderr)
    else:
        for r in results:
            status = r["transit_number"] or "NOT FOUND"
            print(f"{r['file']}: {status}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
