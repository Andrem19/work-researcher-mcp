"""Google Drive sync for the CV folder (account ry4ara@gmail.com, folder "CV").

Read AND write: CVs are pulled into CV_collection for editing (docx via
python-docx) and pushed back with push_cv/upload_cv. Two auth modes:

- oauth: Google Cloud OAuth client (Desktop app) → secrets/google_credentials.json,
  one-time browser consent cached in secrets/google_token.json
  (`work-researcher drive-auth` runs the flow).
- service_account: share the Drive folder with the service-account e-mail and
  drop the JSON key at secrets/google_service_account.json.

Without credentials the server still works: CV_collection is scanned locally
and get_status reports exactly which file is missing.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

from .config import Settings

SCOPES = ["https://www.googleapis.com/auth/drive"]
CV_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/pdf": "pdf",
    "application/msword": "doc",
}
GDOC_EXPORT = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


class DriveNotConfigured(RuntimeError):
    pass


def _creds_path(settings: Settings, key: str) -> Path:
    return settings.project_root / settings.drive.get(key, "")


def build_service(settings: Settings):
    mode = settings.drive.get("mode", "oauth")
    if mode == "service_account":
        sa = _creds_path(settings, "service_account_file")
        if not sa.exists():
            raise DriveNotConfigured(
                f"service account file not found: {sa} — see SETUP.md"
            )
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(
            str(sa), scopes=SCOPES
        )
    else:
        token = _creds_path(settings, "token_file")
        client = _creds_path(settings, "credentials_file")
        if token.exists():
            from google.oauth2.credentials import Credentials

            creds = Credentials.from_authorized_user_file(str(token), SCOPES)
            if creds.expired and creds.refresh_token:
                from google.auth.transport.requests import Request

                creds.refresh(Request())
        elif client.exists():
            raise DriveNotConfigured(
                f"OAuth token missing ({token}). Run: work-researcher drive-auth"
            )
        else:
            raise DriveNotConfigured(
                f"Google credentials not configured: put the OAuth client at {client} "
                f"(or a service account key and switch drive.mode) — see SETUP.md"
            )
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def run_oauth_flow(settings: Settings) -> Path:
    client = _creds_path(settings, "credentials_file")
    if not client.exists():
        raise DriveNotConfigured(
            f"OAuth client file not found: {client}. Create a Desktop-app OAuth client "
            "in Google Cloud Console and download the JSON there (see SETUP.md)."
        )
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(client), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    token_path = _creds_path(settings, "token_file")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return token_path


def _find_folder(service, settings: Settings) -> dict | None:
    folder_id = settings.drive.get("folder_id")
    if folder_id:
        res = service.files().get(fileId=folder_id).execute()
        return res
    name = settings.drive.get("folder_name", "CV")
    res = (
        service.files()
        .list(
            q=f"mimeType='application/vnd.google-apps.folder' and name='{name}' "
            "and trashed=false",
            spaces="drive",
            fields="files(id,name,parents)",
            pageSize=10,
        )
        .execute()
    )
    files = res.get("files", [])
    return files[0] if files else None


def _list_files_sync(settings: Settings) -> dict:
    service = build_service(settings)
    folder = _find_folder(service, settings)
    if folder is None:
        return {
            "ok": False,
            "error": f"Drive folder '{settings.drive.get('folder_name', 'CV')}' not found "
            f"on account {settings.drive.get('account')}",
        }
    files, token = [], None
    while True:
        res = (
            service.files()
            .list(
                q=f"'{folder['id']}' in parents and trashed=false",
                fields="nextPageToken, files(id,name,mimeType,size,modifiedTime,webViewLink)",
                pageSize=200,
                pageToken=token,
            )
            .execute()
        )
        files.extend(res.get("files", []))
        token = res.get("nextPageToken")
        if not token:
            break
    return {"ok": True, "folder": folder, "files": files}


def _download_sync(settings: Settings, file_meta: dict) -> tuple[Path, bool]:
    service = build_service(settings)
    name = file_meta["name"]
    mime = file_meta.get("mimeType", "")
    target = settings.cv_dir / Path(name).name
    exists = target.exists()
    local_stamp = (
        f"{target.stat().st_size}" if exists else ""
    )
    remote_size = file_meta.get("size")
    if exists and remote_size and local_stamp == remote_size:
        return target, False  # unchanged
    if mime == "application/vnd.google-apps.document":
        request = service.files().export_media(fileId=file_meta["id"], mimeType=GDOC_EXPORT)
        target = target.with_suffix(".docx")
    else:
        request = service.files().get_media(fileId=file_meta["id"])
    buf = io.BytesIO()
    from googleapiclient.http import MediaIoBaseDownload

    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    settings.cv_dir.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(buf.getvalue())
    tmp.replace(target)
    return target, True


def _get_downloader():
    from googleapiclient.http import MediaIoBaseDownload

    return MediaIoBaseDownload


def _upload_sync(settings: Settings, path: Path, drive_file_id: str | None) -> dict:
    """Update an existing Drive file (or create it in the CV folder)."""
    from googleapiclient.http import MediaFileUpload

    service = build_service(settings)
    media = MediaFileUpload(
        str(path),
        mimetype=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if path.suffix.lower() == ".docx" else
            "application/pdf" if path.suffix.lower() == ".pdf" else
            "application/msword"
        ),
        resumable=True,
    )
    if drive_file_id:
        meta = service.files().update(
            fileId=drive_file_id, media_body=media
        ).execute()
        return {"action": "updated", "id": meta["id"], "name": meta.get("name"),
                "modified": meta.get("modifiedTime")}
    folder = _find_folder(service, settings)
    if folder is None:
        return {"error": f"Drive folder '{settings.drive.get('folder_name', 'CV')}' "
                "not found — cannot create the file"}
    meta = service.files().create(
        body={"name": path.name, "parents": [folder["id"]]},
        media_body=media,
    ).execute()
    return {"action": "created", "id": meta["id"], "name": meta.get("name"),
            "modified": meta.get("modifiedTime")}


async def upload_cv(settings: Settings, path: str | Path,
                    force: bool = False) -> dict:
    """Push a locally edited CV back to Drive.

    Matches the Drive file via the cvs index (drive_file_id) or by filename
    inside the CV folder. Refuses to overwrite when the Drive copy is newer
    than our last sync (unless force) — pull first, merge, then push.
    """
    from . import persistence as db

    p = Path(path)
    if not p.exists():
        return {"error": f"file not found: {p}"}
    drive_file_id: str | None = None
    remote_modified: str | None = None
    row = None
    async with db.connect(settings.db_path) as conn:
        cur = await conn.execute(
            "SELECT drive_file_id, drive_modified, mtime, path FROM cvs WHERE path=?",
            (str(p),),
        )
        row = await cur.fetchone()
        if row and row["drive_file_id"]:
            drive_file_id = row["drive_file_id"]
            remote_modified = row["drive_modified"]
    if not drive_file_id:
        # unknown or unbound locally: match by name in Drive
        listing = await list_files(settings)
        if not listing.get("ok"):
            return listing
        for f in listing["files"]:
            if f["name"] == p.name:
                drive_file_id = f["id"]
                if not remote_modified:
                    remote_modified = f.get("modifiedTime")
                break
    if remote_modified and not force:
        from .textutils import parse_dt

        remote_dt = parse_dt(remote_modified.replace("Z", "+00:00")) if remote_modified else None
        local_dt = None
        if row and row["drive_modified"]:
            local_dt = parse_dt(row["drive_modified"].replace("Z", "+00:00"))
        if remote_dt and local_dt and remote_dt > local_dt and row and row["mtime"] and \
                row["mtime"] == f"{p.stat().st_size}:{int(p.stat().st_mtime)}":
            return {
                "error": "Drive copy changed after our last sync — run "
                "sync_cvs_from_drive, merge your edits, then push again (or force=true)",
                "drive_modified": remote_modified,
            }
    result = await asyncio.to_thread(_upload_sync, settings, p, drive_file_id)
    if result.get("id"):
        async with db.connect(settings.db_path) as conn:
            await conn.execute(
                """UPDATE cvs SET drive_file_id=?, drive_modified=? WHERE path=?""",
                (result["id"], result.get("modified"), str(p)),
            )
            await conn.commit()
    return result


async def status(settings: Settings) -> dict:
    mode = settings.drive.get("mode", "oauth")
    if not settings.drive.get("enabled", True) or mode == "off":
        return {"enabled": False, "configured": False,
                "note": "Drive sync disabled in config.toml"}
    try:
        await asyncio.to_thread(build_service, settings)
        return {"enabled": True, "configured": True, "mode": mode,
                "account": settings.drive.get("account"),
                "folder_name": settings.drive.get("folder_name")}
    except DriveNotConfigured as exc:
        return {"enabled": True, "configured": False, "mode": mode,
                "setup_needed": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"enabled": True, "configured": False, "mode": mode,
                "error": f"{type(exc).__name__}: {exc}"}


async def list_files(settings: Settings) -> dict:
    return await asyncio.to_thread(_list_files_sync, settings)


async def sync(settings: Settings) -> dict:
    """Pull new/changed CVs from Drive into CV_collection, then re-index."""
    from . import persistence as db
    from .cvmanager import index_cvs

    listing = await list_files(settings)
    if not listing.get("ok"):
        return listing
    supported = [f for f in listing["files"]
                 if f.get("mimeType") in CV_MIMES
                 or f.get("mimeType") == "application/vnd.google-apps.document"
                 or Path(f["name"]).suffix.lower() in {".docx", ".pdf", ".doc"}]
    downloaded, unchanged, skipped = [], [], []
    meta_by_name: dict[str, dict] = {}
    async with db.connect(settings.db_path) as conn:
        for meta in supported:
            try:
                target, changed = await asyncio.to_thread(_download_sync, settings, meta)
                (downloaded if changed else unchanged).append(target.name)
                meta_by_name[target.name] = meta
            except Exception as exc:  # noqa: BLE001
                skipped.append(f"{meta['name']}: {exc}")
        await conn.commit()
    index = await index_cvs(settings)
    # bind Drive ids to the freshly indexed rows (by filename → path)
    async with db.connect(settings.db_path) as conn:
        for name, meta in meta_by_name.items():
            path = str(settings.cv_dir / name)
            await conn.execute(
                """INSERT INTO cvs (id, filename, path, indexed_at)
                   VALUES (?, ?, ?, datetime('now'))
                   ON CONFLICT(path) DO NOTHING""",
                (f"cv_{meta['id'][:12]}", name, path),
            )
            await conn.execute(
                "UPDATE cvs SET drive_file_id=?, drive_modified=? WHERE path=?",
                (meta["id"], meta.get("modifiedTime"), path),
            )
        await conn.commit()
    return {
        "ok": True,
        "folder": listing["folder"]["name"],
        "drive_files": len(listing["files"]),
        "downloaded": downloaded,
        "unchanged": unchanged,
        "skipped": skipped,
        "index": index,
    }
