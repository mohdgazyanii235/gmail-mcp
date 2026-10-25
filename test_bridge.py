"""Run with: python3 test_bridge.py   (standard library only; stubs Google)"""
import json
import sys
import threading
import types
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.modules.setdefault("accounts", types.SimpleNamespace(
    BASE=__import__("pathlib").Path(__import__("tempfile").mkdtemp()), list_aliases=lambda: ["personal", "sales"],
    read_info=lambda a: {"email": a + "@x.com"}, get_service=None))
import bridge as b  # noqa: E402
import test_organise as t  # noqa: E402


class LabelsApi:
    def list(self, userId):
        return t._Exec({"labels": [{"name": "Suppliers", "id": "L1", "type": "user"}]})


class Proxy:
    """The fake Gmail client, plus a labels() call (the fake keeps its label data in an attribute of that name)."""

    def __init__(self, svc):
        self.svc = svc

    def users(self):
        return self

    def labels(self):
        return LabelsApi()

    def messages(self):
        return self.svc


def runner_with(svc, allowed=("personal",)):
    proxy = Proxy(svc)
    h = t.hist()
    return lambda op, body: b.run(op, body, get_service=lambda a: (proxy, ""), history=h, allowed=lambda: list(allowed))


def test_accounts_and_unknown_account():
    r = runner_with(None, ("personal",))
    assert r("accounts", {})["accounts"][0]["account"] == "personal"
    try:
        r("mark_read", {"account": "sales"})
        assert False
    except ValueError as e:
        assert "Unknown account" in str(e)


def test_mark_everything_read_in_inbox():
    svc = t.ListSvc({f"m{i}": ["INBOX", "UNREAD"] for i in range(7)})
    r = runner_with(svc)
    out = r("mark_read", {"account": "personal"})
    assert out["changed"] == 7 and all("UNREAD" not in l for l in svc.labels.values())
    assert r("undo", {"account": "personal"})["messages"] == 7


def test_nothing_dangerous_exists():
    r = runner_with(t.ListSvc({}))
    for op in ("send", "send_message", "delete", "trash", "create_draft", "forward"):
        try:
            r(op, {"account": "personal"})
            assert False, op
        except ValueError:
            pass
    for dest in ("trash", "bin", "sent", "drafts"):
        try:
            r("move", {"account": "personal", "to_folder": dest, "query": "in:inbox"})
            assert False, dest
        except ValueError:
            pass
    try:
        r("create_label", {"account": "personal", "name": "Trash"})
        assert False
    except ValueError:
        pass


def test_move_by_search_to_archive():
    svc = t.ListSvc({"a": ["INBOX"], "b": ["INBOX"]})
    out = runner_with(svc)("move", {"account": "personal", "to_folder": "archive", "query": "in:inbox"})
    assert out["changed"] == 2 and "INBOX" not in svc.labels["a"]


def test_http_needs_the_token_and_reports_errors():
    svc = t.ListSvc({"a": ["INBOX", "UNREAD"]})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), b.make_handler("x" * 40, runner_with(svc)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/mail/mark_read"

    def call(tok, body):
        req = urllib.request.Request(url, json.dumps(body).encode(), {"Authorization": f"Bearer {tok}"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    assert call("wrong", {})[0] == 403
    code, res = call("x" * 40, {"account": "personal"})
    assert code == 200 and res["result"]["changed"] == 1
    assert call("x" * 40, {"account": "nobody"})[0] == 400
    srv.shutdown()


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn) and fn.__module__ == "__main__":
            fn()
            n += 1
    print(f"{n} tests passed")
