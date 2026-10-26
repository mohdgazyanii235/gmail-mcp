"""Run with: python3 test_drivelib.py   (standard library only)"""
import io
import re
import zipfile
from pathlib import Path

import drivelib as d


class Ex:
    def __init__(self, v):
        self.v = v

    def execute(self):
        return self.v


class FakeDrive:
    def __init__(self, files=None, content=None):
        self.files_by_id = files or {}
        self.content = content or {}
        self.calls = []

    def files(self):
        return self

    def list(self, **kw):
        self.calls.append(("list", kw))
        return Ex({"files": list(self.files_by_id.values())})

    def get(self, fileId, **kw):
        return Ex(self.files_by_id[fileId])

    def export(self, fileId, mimeType):
        self.calls.append(("export", mimeType))
        return Ex(self.content[fileId])

    def get_media(self, fileId, **kw):
        self.calls.append(("media", fileId))
        return Ex(self.content[fileId])


def f(i, name, mime, **kw):
    return {"id": i, "name": name, "mimeType": mime, "modifiedTime": "2026-10-20T10:00:00Z", "owners": [{"displayName": "Mo"}], "webViewLink": "https://x/" + i, **kw}


def test_query_is_escaped_and_scoped():
    q = d.build_query("Fan's \\ plan", kind="sheet", folder_id="abc12345", modified_after="2026-10-01")
    assert q.startswith("trashed = false and ")
    assert "Fan\\'s \\\\ plan" in q and "'abc12345' in parents" in q and "modifiedTime > '2026-10-01T00:00:00'" in q
    assert "' or " not in q.split("name contains")[1].split(")")[0].replace("' or fullText", "")  # nothing broke out of the literal
    for bad in (dict(kind="movie"), dict(folder_id="a b"), dict(modified_after="yesterday")):
        try:
            d.build_query("x", **bad)
            assert False, bad
        except ValueError:
            pass


def test_search_covers_shared_drives_and_summarises():
    svc = FakeDrive({"a1b2c3": f("a1b2c3", "Wholesale plan", "application/vnd.google-apps.document")})
    out = d.search(svc, "wholesale", max_results=500)
    assert out == [{"id": "a1b2c3", "name": "Wholesale plan", "type": "doc", "modified": "2026-10-20", "owner": "Mo", "link": "https://x/a1b2c3"}]
    kw = svc.calls[0][1]
    assert kw["corpora"] == "allDrives" and kw["supportsAllDrives"] and kw["pageSize"] == 50


def test_read_google_doc_sheet_text_docx():
    docx = io.BytesIO()
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                   "<w:p><w:r><w:t>Hello</w:t></w:r></w:p><w:p><w:r><w:t>World</w:t></w:r></w:p></w:body></w:document>")
    svc = FakeDrive({
        "doc11": f("doc11", "Plan", "application/vnd.google-apps.document"),
        "sht11": f("sht11", "Pricing", "application/vnd.google-apps.spreadsheet"),
        "txt11": f("txt11", "notes.txt", "text/plain", size="20"),
        "dcx11": f("dcx11", "brief.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", size="900"),
    }, {"doc11": b"Plan text", "sht11": b"a,b\n1,2", "txt11": b"plain notes", "dcx11": docx.getvalue()})
    assert d.read(svc, "doc11")["content"] == "Plan text"
    r = d.read(svc, "sht11")
    assert r["content"] == "a,b\n1,2" and "first sheet" in r["note"]
    assert d.read(svc, "txt11")["content"] == "plain notes"
    assert d.read(svc, "dcx11")["content"] == "Hello\nWorld"
    assert ("export", "text/csv") in svc.calls
    assert d.read(svc, "doc11", max_chars=500)["truncated"] is False


def test_read_refuses_what_it_cannot_or_should_not():
    svc = FakeDrive({"fold1": f("fold1", "Docs", d.FOLDER), "img11": f("img11", "p.png", "image/png"),
                     "big11": f("big11", "big.txt", "text/plain", size=str(d.MAX_DOWNLOAD + 1))})
    for fid in ("fold1", "img11", "big11", "bad id", "../x"):
        try:
            d.read(svc, fid)
            assert False, fid
        except ValueError:
            pass
    assert ("media", "big11") not in svc.calls  # too big: never downloaded


def test_truncation():
    svc = FakeDrive({"doc11": f("doc11", "Long", "application/vnd.google-apps.document")}, {"doc11": ("x" * 3000).encode()})
    r = d.read(svc, "doc11", max_chars=1000)
    assert len(r["content"]) == 1000 and r["truncated"]


def test_nothing_in_the_drive_code_can_write():
    """Belt and braces: the login only has drive.readonly, and the code must not even call a write method."""
    src = Path("drivelib.py").read_text() + Path("accounts.py").read_text()
    assert "drive.readonly" in src and "auth/drive\"" not in src and "auth/drive'" not in src
    for verb in (".create(", ".update(", ".delete(", ".copy(", ".emptyTrash(", "permissions(", ".trash("):
        assert verb not in Path("drivelib.py").read_text(), verb
    server = Path("server.py").read_text()
    drive_part = server[server.index("Tools: Google Drive"):server.index("# --- ASGI app")]
    assert drive_part.count("@mcp.tool(annotations=READ)") == 4 and "DRAFT" not in drive_part and "SEND" not in drive_part


def test_drive_tokens_are_kept_apart_from_gmail_logins():
    src = Path("accounts.py").read_text()
    assert 'DRIVE_TOKENS = BASE / "drive_tokens"' in src and 'TOKENS = BASE / "tokens"' in src


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"{n} tests passed")
