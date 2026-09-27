"""ENG-269: the public_folder Drive mode — anonymous read-only CV sync.

Every requirement the issue names, exercised against a fake Google over
httpx.MockTransport (no network, no credentials, no secret files):

- the public folder config needs only the URL/id, never credentials;
- anonymous listing + download through the real code path;
- a second sync dedupes (unchanged) instead of duplicating;
- a changed file on Drive refreshes the local copy;
- unsupported file types are skipped;
- push_cv_to_drive fails closed as read-only;
- network failure is an honest `ok: False`, not a crash;
- no secret files are required anywhere on the path.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import asyncio

import httpx
import pytest

from work_researcher.config import Settings
from work_researcher import drive as drive_mod

# Synthetic id: the real folder id is a capability (anyone-with-the-link)
# and must never be committed to this public repository.
FOLDER_ID = "1SyntheticFolderIdForTestsOnly000000aa"

DOCX_A = b"PK\x03\x04 docx-bytes-version-1" + b"A" * 128
DOCX_B = b"PK\x03\x04 docx-bytes-version-2" + b"B" * 128  # changed content, same size
PDF_C = b"%PDF-1.7 geo-cv-bytes"

FILES = {
    "1C-EPfw9jNEm94QIpjW89PXWxVoKRP3Zg": ("Andrew_Remniow_CV_Data_Analytics.docx", DOCX_A),
    "1pLauXGAd-Z9SGsacZisp0BXj6qhpaYnJ": ("cv_geo.pdf", PDF_C),
}


def escaped(blob: str) -> str:
    """The way the folder page wraps its listing data in JS string escapes."""
    out = []
    for ch in blob:
        if ch in "\"[]\\":
            out.append("\\x%02x" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def folder_page(extra_rows: list[str] | None = None) -> str:
    # One escape pass: the raw row text is wrapped in JS string escapes exactly
    # like the real page's `_DRIVE_ivd` assignment.
    raw_rows = [
        f'["{fid}",["{FOLDER_ID}"],"{name}","{DOCX_MIME}",'
        f'0,null,0,0,0,1788015424505,1788224676850,null,null,{len(body)},[[1,2]]],'
        for fid, (name, body) in FILES.items()
    ] + (extra_rows or [])
    ivd = escaped("[[" + "".join(raw_rows).rstrip(",") + "]]")
    return (
        "<html><script>window['_DRIVE_ivd'] = '"
        + ivd
        + "';if (window['_DRIVE_ivd']) {window['_DRIVE_ivd']();}</script></html>"
    )


def fake_google(changed: dict[str, bytes] | None = None, down: bool = False):
    """An httpx.MockTransport serving the folder page and the file bytes."""
    changed = changed or {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(drive_mod.FOLDER_PAGE_URL):
            if down:
                raise httpx.ConnectError("network is down")
            return httpx.Response(200, text=folder_page())
        if url.startswith(drive_mod.DOWNLOAD_URL):
            fid = dict(pair.split("=") for pair in url.split("?")[1].split("&"))["id"]
            body = changed.get(fid, FILES[fid][1])
            return httpx.Response(
                200, content=body, headers={"content-type": "application/octet-stream"}
            )
        return httpx.Response(404, text="nope")

    return httpx.MockTransport(handler)


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=tmp_path,
        config_path=tmp_path / "config.toml",
        data_dir=tmp_path / "data",
        cv_dir=tmp_path / "CV_collection",
        db_path=tmp_path / "data" / "work_researcher.db",
        drive={
            "enabled": True,
            "mode": "public_folder",
            "folder_url": f"https://drive.google.com/drive/folders/{FOLDER_ID}",
            "folder_name": "CV",
        },
    )


@pytest.fixture()
def public_settings(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr(drive_mod, "_client", lambda timeout_s: httpx.Client(
        transport=fake_google(), timeout=timeout_s, follow_redirects=True,
        headers={"user-agent": "test"}))
    return settings


def test_listing_parses_anonymous_folder_page(public_settings: Settings) -> None:
    listing = asyncio.run(drive_mod.list_files(public_settings))
    assert listing["ok"] is True
    assert listing["folder"]["id"] == FOLDER_ID
    assert {f["name"] for f in listing["files"]} == {name for name, _ in FILES.values()}
    # every entry carries the stable Drive file id and the modified stamp
    for entry in listing["files"]:
        assert entry["id"] in FILES
        assert entry.get("modifiedTime")


def test_sync_downloads_supported_files_and_dedupes(public_settings: Settings) -> None:
    first = asyncio.run(drive_mod.sync(public_settings))
    assert first["ok"] is True
    assert sorted(first["downloaded"]) == ["Andrew_Remniow_CV_Data_Analytics.docx", "cv_geo.pdf"]
    assert first["unchanged"] == []

    # A second sync re-downloads to hash-verify but records no copy and no change.
    second = asyncio.run(drive_mod.sync(public_settings))
    assert second["ok"] is True
    assert second["downloaded"] == []
    assert sorted(second["unchanged"]) == ["Andrew_Remniow_CV_Data_Analytics.docx", "cv_geo.pdf"]
    # exactly one file per Drive entry on disk
    on_disk = sorted(p.name for p in public_settings.cv_dir.iterdir())
    assert on_disk == ["Andrew_Remniow_CV_Data_Analytics.docx", "cv_geo.pdf"]


def test_sync_persists_stable_drive_ids(public_settings: Settings) -> None:
    from work_researcher import persistence as db

    asyncio.run(drive_mod.sync(public_settings))
    async def rows():
        async with db.connect(public_settings.db_path) as conn:
            cur = await conn.execute(
                "SELECT filename, drive_file_id, drive_modified FROM cvs ORDER BY filename")
            return await cur.fetchall()
    found = asyncio.run(rows())
    by_name = {r["filename"]: r for r in found}
    assert by_name["Andrew_Remniow_CV_Data_Analytics.docx"]["drive_file_id"] in FILES
    assert by_name["cv_geo.pdf"]["drive_modified"]


def test_changed_file_is_refreshed(public_settings: Settings, monkeypatch) -> None:
    asyncio.run(drive_mod.sync(public_settings))
    target = public_settings.cv_dir / "Andrew_Remniow_CV_Data_Analytics.docx"
    before = target.read_bytes()

    fid = "1C-EPfw9jNEm94QIpjW89PXWxVoKRP3Zg"
    monkeypatch.setattr(drive_mod, "_client", lambda timeout_s: httpx.Client(
        transport=fake_google(changed={fid: DOCX_B}), timeout=timeout_s,
        follow_redirects=True, headers={"user-agent": "test"}))
    result = asyncio.run(drive_mod.sync(public_settings))
    assert "Andrew_Remniow_CV_Data_Analytics.docx" in result["downloaded"]
    assert target.read_bytes() == DOCX_B
    assert target.read_bytes() != before


def test_unsupported_types_are_skipped(public_settings: Settings, monkeypatch) -> None:
    gsheet_row = (
        f'["1aaaaBBBBccccDDDDeeee",["{FOLDER_ID}"],"notes.gsheet",'
        f'"application/vnd.google-apps.spreadsheet",'
        f'0,null,0,0,0,1788015424505,1788224676850,null,null,10,[[1,2]]],'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(drive_mod.FOLDER_PAGE_URL):
            return httpx.Response(200, text=folder_page(extra_rows=[gsheet_row]))
        return fake_google()(request)

    monkeypatch.setattr(drive_mod, "_client", lambda timeout_s: httpx.Client(
        transport=httpx.MockTransport(handler), timeout=timeout_s,
        follow_redirects=True, headers={"user-agent": "test"}))

    result = asyncio.run(drive_mod.sync(public_settings))
    assert result["ok"] is True
    assert result["drive_files"] == 3  # listing saw the sheet too
    assert any("notes.gsheet" in entry for entry in result["skipped"])
    assert not (public_settings.cv_dir / "notes.gsheet").exists()


def test_push_cv_fails_closed_read_only(public_settings: Settings, tmp_path: Path) -> None:
    local = tmp_path / "edited.docx"
    local.write_bytes(DOCX_B)
    with pytest.raises(drive_mod.DriveReadOnly):
        asyncio.run(drive_mod.upload_cv(public_settings, local))


def test_network_failure_is_honest(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(drive_mod, "_client", lambda timeout_s: httpx.Client(
        transport=fake_google(down=True), timeout=timeout_s,
        follow_redirects=True, headers={"user-agent": "test"}))
    result = asyncio.run(drive_mod.sync(settings))
    assert result["ok"] is False
    assert "unreachable" in result["error"]


def test_no_secret_files_are_required(tmp_path: Path, monkeypatch) -> None:
    """The whole public path configures and lists without any secrets/ file."""
    settings = make_settings(tmp_path)
    assert not (tmp_path / "secrets").exists()
    status = asyncio.run(drive_mod.status(settings))
    assert status["configured"] is True
    assert status["mode"] == "public_folder"
    assert status["read_only"] is True
    monkeypatch.setattr(drive_mod, "_client", lambda timeout_s: httpx.Client(
        transport=fake_google(), timeout=timeout_s, follow_redirects=True,
        headers={"user-agent": "test"}))
    listing = asyncio.run(drive_mod.list_files(settings))
    assert listing["ok"] is True


def test_folder_url_and_id_both_resolve(tmp_path: Path) -> None:
    by_url = Settings(project_root=tmp_path, config_path=tmp_path / "c.toml",
                      drive={"mode": "public_folder", "folder_url": f"https://drive.google.com/drive/folders/{FOLDER_ID}"})
    by_id = Settings(project_root=tmp_path, config_path=tmp_path / "c.toml",
                     drive={"mode": "public_folder", "folder_id": FOLDER_ID})
    none = Settings(project_root=tmp_path, config_path=tmp_path / "c.toml",
                    drive={"mode": "public_folder"})
    assert drive_mod.public_folder_id(by_url) == FOLDER_ID
    assert drive_mod.public_folder_id(by_id) == FOLDER_ID
    assert drive_mod.public_folder_id(none) is None
    status = asyncio.run(drive_mod.status(none))
    assert status["configured"] is False
    assert "folder_url" in status["setup_needed"]


def test_download_confirmation_interstitial_is_followed(public_settings: Settings, monkeypatch) -> None:
    """Google's virus-scan interstitial carries a form; the download retries
    with its fields and lands on the file bytes (the branch the real endpoint
    takes for larger files)."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(drive_mod.FOLDER_PAGE_URL):
            return httpx.Response(200, text=folder_page())
        if url.startswith(drive_mod.DOWNLOAD_URL):
            seen.append(url)
            if "uuid" not in url:
                return httpx.Response(200, text=(
                    '<form action="' + drive_mod.DOWNLOAD_URL + '" method="get">'
                    '<input type="hidden" name="uuid" value="uuu">'
                    '<input type="hidden" name="confirm" value="t">'
                    '<input type="hidden" name="id" value="1C-EPfw9jNEm94QIpjW89PXWxVoKRP3Zg">'
                    "</form>"), headers={"content-type": "text/html"})
            return httpx.Response(200, content=DOCX_A, headers={"content-type": "application/octet-stream"})
        return httpx.Response(404)

    monkeypatch.setattr(drive_mod, "_client", lambda timeout_s: httpx.Client(
        transport=httpx.MockTransport(handler), timeout=timeout_s,
        follow_redirects=True, headers={"user-agent": "test"}))

    result = asyncio.run(drive_mod.sync(public_settings))
    assert result["ok"] is True
    # both files hit the interstitial once and retried with its form fields
    assert len(seen) == 4
    assert (public_settings.cv_dir / "Andrew_Remniow_CV_Data_Analytics.docx").read_bytes() == DOCX_A


def test_a_revoked_public_link_answers_honestly(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    monkeypatch.setattr(drive_mod, "_client", lambda timeout_s: httpx.Client(
        transport=httpx.MockTransport(handler), timeout=timeout_s,
        follow_redirects=True, headers={"user-agent": "test"}))
    result = asyncio.run(drive_mod.sync(settings))
    assert result["ok"] is False
    assert "404" in result["error"]
