"""
Upload data/ CSVs to Google Drive under leads/{date}/.

Requires env vars:
  GOOGLE_DRIVE_CREDENTIALS  - contents of the service account JSON key
  GOOGLE_DRIVE_FOLDER_ID    - ID of the root 'leads' folder
"""

import datetime
import json
import os
import pathlib

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


SCOPES = ["https://www.googleapis.com/auth/drive"]
DATA_DIR = pathlib.Path("data")


def _build_service():
    raw = os.environ["GOOGLE_DRIVE_CREDENTIALS"]
    info = json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _get_or_create_folder(service, name: str, parent_id: str) -> str:
    query = (
        f"name='{name}' and mimeType='application/vnd.google-apps.folder'"
        f" and '{parent_id}' in parents and trashed=false"
    )
    results = service.files().list(
        q=query, fields="files(id)",
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=meta, fields="id", supportsAllDrives=True).execute()
    return folder["id"]


def upload(date_str: str | None = None) -> None:
    root_folder_id = os.environ["GOOGLE_DRIVE_FOLDER_ID"]
    if not date_str:
        date_str = datetime.date.today().isoformat()

    service = _build_service()
    date_folder_id = _get_or_create_folder(service, date_str, root_folder_id)

    csvs = sorted(DATA_DIR.glob("*.csv"))
    if not csvs:
        print("[Drive] No CSVs found in data/ to upload.")
        return

    print(f"[Drive] Uploading {len(csvs)} files to leads/{date_str}/...")
    for csv_path in csvs:
        media = MediaFileUpload(str(csv_path), mimetype="text/csv", resumable=False)
        meta = {"name": csv_path.name, "parents": [date_folder_id]}

        # Overwrite if file already exists in this folder
        query = (
            f"name='{csv_path.name}' and '{date_folder_id}' in parents and trashed=false"
        )
        existing = service.files().list(
            q=query, fields="files(id)",
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute().get("files", [])
        if existing:
            service.files().update(
                fileId=existing[0]["id"], media_body=media, supportsAllDrives=True,
            ).execute()
        else:
            service.files().create(
                body=meta, media_body=media, fields="id", supportsAllDrives=True,
            ).execute()

        print(f"[Drive]   {csv_path.name}")

    print(f"[Drive] Done. Folder: leads/{date_str}/")


if __name__ == "__main__":
    upload()
