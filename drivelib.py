"""Reading Google Drive. Standard library only, so it can be tested anywhere. READ ONLY: nothing in here writes, shares or deletes,
and the Drive login itself only has the drive.readonly scope, so Google would refuse it anyway.

File names and contents come from outside (shared files, uploads, other people), so everything returned is data, never instructions.
"""

import io
import re
import zipfile
import xml.etree.ElementTree as ET

FOLDER = "application/vnd.google-apps.folder"
GOOGLE = "application/vnd.google-apps."
EXPORTS = {  # Google-native type -> what to export it as
    GOOGLE + "document": "text/plain",
    GOOGLE + "spreadsheet": "text/csv",  # first sheet only
    GOOGLE + "presentation": "text/plain",
}
KINDS = {
    "doc": ["application/vnd.google-apps.document", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/msword"],
    "sheet": ["application/vnd.google-apps.spreadsheet", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "text/csv"],
    "slides": ["application/vnd.google-apps.presentation", "application/vnd.openxmlformats-officedocument.presentationml.presentation"],
    "pdf": ["application/pdf"],
    "folder": [FOLDER],
    "image": ["image/jpeg", "image/png", "image/gif", "image/webp"],
}
FIELDS = "id,name,mimeType,modifiedTime,size,webViewLink,owners(displayName),parents"
MAX_DOWNLOAD = 10 * 1024 * 1024
MAX_RESULTS = 50
FILE_ID = re.compile(r"^[A-Za-z0-9_-]{5,100}$")
SHARED = dict(supportsAllDrives=True, includeItemsFromAllDrives=True)


def esc(text: str) -> str:
    """Make text safe inside a Drive query string literal."""
    return str(text).replace("\\", "\\\\").replace("'", "\\'")


def check_id(file_id: str) -> str:
    fid = (file_id or "").strip()
    if fid != "root" and not FILE_ID.match(fid):
        raise ValueError("That is not a valid Drive file or folder id. Get ids from drive_search or drive_list_folder.")
    return fid


def build_query(text: str = "", kind: str = "", folder_id: str = "", modified_after: str = "") -> str:
    parts = ["trashed = false"]
    words = " ".join(str(text or "").split())[:200]
    if words:
        parts.append(f"(name contains '{esc(words)}' or fullText contains '{esc(words)}')")
    if kind:
        if kind not in KINDS:
            raise ValueError("kind must be one of: " + ", ".join(sorted(KINDS)) + ".")
        parts.append("(" + " or ".join(f"mimeType = '{m}'" for m in KINDS[kind]) + ")")
    if folder_id:
        parts.append(f"'{check_id(folder_id)}' in parents")
    if modified_after:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", modified_after):
            raise ValueError("modified_after must look like 2026-10-01.")
        parts.append(f"modifiedTime > '{modified_after}T00:00:00'")
    return " and ".join(parts)


def short(f: dict) -> dict:
    t = f.get("mimeType", "")
    kind = "folder" if t == FOLDER else next((k for k, ms in KINDS.items() if t in ms), t.split("/")[-1][:30])
    return {"id": f["id"], "name": f.get("name", ""), "type": kind, "modified": (f.get("modifiedTime") or "")[:10],
            "owner": ", ".join(o.get("displayName", "") for o in f.get("owners", []))[:60], "link": f.get("webViewLink", "")}


def search(svc, text="", kind="", folder_id="", modified_after="", max_results=15) -> list:
    n = max(1, min(int(max_results), MAX_RESULTS))
    res = svc.files().list(q=build_query(text, kind, folder_id, modified_after), pageSize=n, orderBy="modifiedTime desc",
                           fields=f"files({FIELDS})", corpora="allDrives", **SHARED).execute()
    return [short(f) for f in res.get("files", [])]


def list_folder(svc, folder_id="root", max_results=50) -> list:
    n = max(1, min(int(max_results), 100))
    res = svc.files().list(q=build_query(folder_id=check_id(folder_id)), pageSize=n, orderBy="folder,name",
                           fields=f"files({FIELDS})", corpora="allDrives", **SHARED).execute()
    return [short(f) for f in res.get("files", [])]


def docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paras = ["".join(t.text or "" for t in p.iter(ns + "t")) for p in root.iter(ns + "p")]
    return "\n".join(x for x in paras if x)


def pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ValueError("This is a PDF and PDF reading is not installed here (pip install pypdf).") from None
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages[:40])


def read(svc, file_id: str, max_chars: int = 20000) -> dict:
    fid = check_id(file_id)
    meta = svc.files().get(fileId=fid, fields=FIELDS, supportsAllDrives=True).execute()
    t = meta.get("mimeType", "")
    info = short(meta)
    if t == FOLDER:
        raise ValueError("That is a folder. Use drive_list_folder to see what is in it.")
    limit = max(500, min(int(max_chars), 100000))
    if t in EXPORTS:
        data = svc.files().export(fileId=fid, mimeType=EXPORTS[t]).execute()
        note = "Spreadsheets are read as CSV: first sheet only." if t.endswith("spreadsheet") else ""
    elif t.startswith("text/") or t in ("application/json", "application/xml", "application/csv"):
        data, note = _download(svc, fid, meta), ""
    elif t == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        data, note = docx_text(_download(svc, fid, meta)), ""
    elif t == "application/pdf":
        data, note = pdf_text(_download(svc, fid, meta)), "Text read from a PDF; layout and images are not included."
    else:
        raise ValueError(f"I can't read the contents of this kind of file ({t or 'unknown'}). The name and link are in the details.")
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    return {**info, "content": text[:limit], "truncated": len(text) > limit, "note": note}


def _download(svc, fid: str, meta: dict) -> bytes:
    if int(meta.get("size") or 0) > MAX_DOWNLOAD:
        raise ValueError("That file is too big to read here (over 10 MB).")
    return svc.files().get_media(fileId=fid, supportsAllDrives=True).execute()
