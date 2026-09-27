"""Google Drive sync for the CV folder.

Three modes:

- public_folder: anonymous, read-only download from a shared-by-link public
  folder. No Google credentials of any kind — the config names the folder
  (URL or id) and that value is not a secret. `push_cv_to_drive` fails closed
  in this mode: the bot only ever downloads. This is the Remtz Hub mode
  (ENG-269): the CV folder is shared "anyone with the link".
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
import re
from datetime import UTC, datetime
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


class DriveReadOnly(RuntimeError):
    """Raised when a write is attempted in the public read-only mode."""

    def __init__(self) -> None:
        super().__init__(
            "public_folder mode is read-only: the bot downloads CVs and never "
            "writes to Drive — edit the file in Google Drive instead"
        )


FOLDER_PAGE_URL = "https://drive.google.com/drive/folders/"
DOWNLOAD_URL = "https://drive.usercontent.google.com/download"
_USER_AGENT = "work-researcher-mcp (public CV sync)"

# One file row of the folder page's embedded `_DRIVE_ivd` array, decoded:
# ["<id>",["<parent>"]," <name>"," <mime>", <n>, null, <n>, <n>, <n>,
#  <created_ms>, <modified_ms>, null, null, <size>, [[...
_ROW = re.compile(
    r'\["(?P<id>[-A-Za-z0-9_]{20,})",'  # file id
    r'\["[-A-Za-z0-9_]{20,}"\],'  # parent folder id
    r'"(?P<name>.*?)",'  # file name; lazy — an embedded quote resolves by backtracking
    r'"(?P<mime>[^"]*)"'  # mime type
    r'(?:,\d+,null,\d+,\d+,\d+,(?P<created>\d{12,}),(?P<modified>\d{12,}),null,null,'
    r'(?P<size>\d+),\[\[)?'  # the numeric tail is optional per row
    ,
    re.S,  # the decoded blob is one long line; a lazy name must be able to span it
)

_JS_ESCAPE = re.compile(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.)", re.S)


def _unescape_js(blob: str) -> str:
    r"""Decode the JS string escapes (hex, unicode, \n, backslash, quote) the
    folder page wraps its listing data in."""

    def one(match: re.Match[str]) -> str:
        esc = match.group(1)
        if esc[0] in "xu":
            return chr(int(esc[1:], 16))
        return {"n": "\n", "t": "\t", "r": "\r"}.get(esc, esc)

    return _JS_ESCAPE.sub(one, blob)


def _client(timeout_s: float):
    """One HTTP client per operation. Tests substitute a MockTransport here."""
    import httpx

    return httpx.Client(
        timeout=timeout_s,
        follow_redirects=True,
        headers={"user-agent": _USER_AGENT},
    )


def public_folder_id(settings: Settings) -> str | None:
    """The public folder's id, from `folder_id` or parsed from `folder_url`."""
    fid = str(settings.drive.get("folder_id") or "").strip()
    if fid:
        return fid
    url = str(settings.drive.get("folder_url") or "").strip()
    if not url:
        return None
    match = re.search(r"/folders/([-A-Za-z0-9_]{10,})", url) or re.search(
        r"[?&]id=([-A-Za-z0-9_]{10,})", url
    )
    return match.group(1) if match else None


def _extract_ivd_blob(html: str) -> str | None:
    """The escaped data string the folder page assigns to `_DRIVE_ivd`.

    Character-level scan (a backslash always escapes the next character), so
    Google's quoted-`'`-inside-the-data tricks cannot truncate the blob.
    """
    i = html.find("_DRIVE_ivd")
    while i != -1:
        eq = html.find("= '", i)
        if eq == -1:
            return None
        j = eq + 3
        out: list[str] = []
        while j < len(html):
            ch = html[j]
            if ch == "\\":
                out.append(html[j : j + 2])
                j += 2
                continue
            if ch == "'":
                break
            out.append(ch)
            j += 1
        blob = "".join(out)
        if len(blob) > 50:
            return blob
        i = html.find("_DRIVE_ivd", i + 1)
    return None


def _parse_folder_listing(html: str) -> list[dict]:
    """File entries embedded in the anonymous folder page.

    The page serialises the listing as escaped JS; rows carry id, parent, name,
    mime and (when the row is complete) created/modified epoch-ms and size.
    The folder owner's own layout may evolve, so anything this parser cannot
    confidently read is left out and the caller reports the honest count.
    """
    files: list[dict] = []
    seen: set[str] = set()
    blob = _extract_ivd_blob(html)
    if blob is None:
        return files
    decoded = _unescape_js(blob)
    for row in _ROW.finditer(decoded):
        fid = row.group("id")
        if fid in seen:
            continue
        seen.add(fid)
        entry: dict = {
            "id": fid,
            "name": row.group("name"),
            "mimeType": row.group("mime"),
        }
        if row.group("modified"):
            entry["modifiedTime"] = datetime.fromtimestamp(
                int(row.group("modified")) / 1000, tz=UTC
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        if row.group("size"):
            entry["size"] = row.group("size")
        files.append(entry)
    return files


def _public_list_sync(settings: Settings, timeout_s: float = 30.0) -> dict:
    folder_id = public_folder_id(settings)
    if not folder_id:
        return {
            "ok": False,
            "error": "public_folder mode needs the shared folder: set drive.folder_url "
            "(the 'anyone with the link' URL) or drive.folder_id in config.toml",
        }
    with _client(timeout_s) as client:
        try:
            resp = client.get(f"{FOLDER_PAGE_URL}{folder_id}")
        except Exception as exc:  # noqa: BLE001 - network failure is a fact, not a crash
            return {"ok": False, "error": f"folder page unreachable: {type(exc).__name__}: {exc}"}
    if resp.status_code != 200:
        return {
            "ok": False,
            "error": f"folder page answered {resp.status_code} for folder {folder_id} "
            "(is the link shared 'anyone with the link'?)",
        }
    files = _parse_folder_listing(resp.text)
    if not files:
        return {
            "ok": False,
            "error": "the public folder page named no files — the listing layout "
            "may have changed or the folder is empty",
        }
    return {
        "ok": True,
        "folder": {"id": folder_id, "name": settings.drive.get("folder_name", "CV")},
        "files": files,
    }


def _public_download_sync(
    settings: Settings, file_meta: dict, timeout_s: float = 60.0
) -> tuple[Path, bool]:
    """Download one public file; unchanged content is detected by sha256."""
    import hashlib

    file_id = file_meta["id"]
    name = file_meta["name"]
    if not name or Path(name).name in {"", ".", ".."}:
        raise RuntimeError(f"the listing carries an unusable file name: {name!r}")
    target = settings.cv_dir / Path(name).name
    params: dict = {"id": file_id, "export": "download", "confirm": "t"}
    with _client(timeout_s) as client:
        resp = client.get(DOWNLOAD_URL, params=params)
        if resp.status_code != 200:
            raise RuntimeError(f"download answered HTTP {resp.status_code}")
        if resp.headers.get("content-type", "").startswith("text/"):
            # Google's download-confirmation interstitial: re-ask with its form fields.
            extra = {
                key: value
                for key, value in re.findall(
                    r'name="(\w+)"\s+value="([^"]*)"', resp.text
                )
            }
            if not extra:
                raise RuntimeError(
                    "download returned an HTML interstitial with no confirmation form"
                )
            resp = client.get(DOWNLOAD_URL, params={**params, **extra})
            if resp.status_code != 200:
                raise RuntimeError(f"download answered HTTP {resp.status_code} after confirm")
        if "text/html" in resp.headers.get("content-type", ""):
            # A second interstitial (captcha, rate-limit page) must never be
            # written as a CV: hash-detection would otherwise pin the garbage
            # in place as "unchanged" on every later sync.
            raise RuntimeError("download returned an HTML page instead of the file")
        data = resp.content
    if not data:
        raise RuntimeError("download returned an empty body")
    digest = hashlib.sha256(data).hexdigest()
    if target.exists():
        existing = hashlib.sha256(target.read_bytes()).hexdigest()
        if existing == digest:
            return target, False  # unchanged
    settings.cv_dir.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(target)
    return target, True


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
                creds.refresh(None)
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


def _is_public(settings: Settings) -> bool:
    return settings.drive.get("mode") == "public_folder"


def _list_files_sync(settings: Settings) -> dict:
    if _is_public(settings):
        return _public_list_sync(settings)
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
    if _is_public(settings):
        raise DriveReadOnly()
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
    if mode == "public_folder":
        fid = public_folder_id(settings)
        return {
            "enabled": True,
            "configured": fid is not None,
            "mode": mode,
            "folder_id": fid,
            "read_only": True,
            "note": "public shared folder: sync_cvs downloads anonymously; "
            "push_cv_to_drive is not supported",
            **({} if fid else {"setup_needed": "set drive.folder_url or drive.folder_id in config.toml"}),
        }
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
    public = _is_public(settings)
    downloaded, unchanged, skipped = [], [], []
    supported = []
    for entry in listing["files"]:
        if (entry.get("mimeType") in CV_MIMES
                or entry.get("mimeType") == "application/vnd.google-apps.document"
                or Path(entry["name"]).suffix.lower() in {".docx", ".pdf", ".doc"}):
            supported.append(entry)
        else:
            # A file the CV parser cannot use is reported, not silently dropped:
            # the owner sees the folder's real contents versus what landed locally.
            skipped.append(
                f"{entry['name']}: unsupported type ({entry.get('mimeType') or 'unknown'})"
            )
    meta_by_name: dict[str, dict] = {}
    async with db.connect(settings.db_path) as conn:
        for meta in supported:
            try:
                if public:
                    target, changed = await asyncio.to_thread(
                        _public_download_sync, settings, meta
                    )
                else:
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
