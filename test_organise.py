"""Run with: python3 test_organise.py   (standard library only)"""
import os
import tempfile

import organise as o

LABELS = {"suppliers": "Label_1", "invoices/2026": "Label_2"}


class FakeSvc:
    """Just enough of the Gmail client: messages().get and batchModify, keeping labels per message."""

    def __init__(self, labels):
        self.labels = {k: list(v) for k, v in labels.items()}
        self.calls = []

    def users(self):
        return self

    def messages(self):
        return self

    def get(self, userId, id, format, metadataHeaders):
        return _Exec({"id": id, "labelIds": list(self.labels[id]),
                      "payload": {"headers": [{"name": "From", "value": "a@b.com"}, {"name": "Subject", "value": "S " + id}]}})

    def batchModify(self, userId, body):
        def run():
            self.calls.append(body)
            for i in body["ids"]:
                cur = [l for l in self.labels[i] if l not in body["removeLabelIds"]]
                self.labels[i] = cur + [l for l in body["addLabelIds"] if l not in cur]
        return _Run(run)


class _Exec:
    def __init__(self, v):
        self.v = v

    def execute(self):
        return self.v


class _Run:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        self.fn()
        return {}


def hist():
    return o.History(os.path.join(tempfile.mkdtemp(), "h.json"))


def test_ids_validated():
    assert o.check_ids("abc123") == ["abc123"]
    assert o.check_ids(["a", "a", "b"]) == ["a", "b"]
    for bad in ([], [""], ["a b"], ["../x"], [str(i) for i in range(26)]):
        try:
            o.check_ids(bad)
            assert False, bad
        except ValueError:
            pass


def test_targets():
    assert o.resolve_target("INBOX", LABELS) == (["INBOX"], ["SPAM"], "the inbox")
    assert o.resolve_target("Junk", LABELS)[:2] == (["SPAM"], ["INBOX"])
    assert o.resolve_target("spam", LABELS)[:2] == (["SPAM"], ["INBOX"])
    assert o.resolve_target("archive", LABELS)[:2] == ([], ["INBOX"])
    assert o.resolve_target("Suppliers", LABELS)[:2] == (["Label_1"], ["INBOX", "SPAM"])


def test_refused_targets():
    for name in ("trash", "Bin", "delete", "SENT", "drafts", ""):
        try:
            o.resolve_target(name, LABELS)
            assert False, name
        except ValueError:
            pass
    try:
        o.resolve_target("nowhere", LABELS)
        assert False
    except ValueError as e:
        assert "suppliers" in str(e)


def test_diff():
    assert o.diff(["INBOX", "UNREAD"], ["SPAM"], ["INBOX"]) == (["SPAM"], ["INBOX"])
    assert o.diff(["SPAM"], ["SPAM"], ["INBOX"]) == ([], [])


def test_move_and_undo_round_trip():
    svc, h = FakeSvc({"m1": ["INBOX", "UNREAD"], "m2": ["INBOX"], "m3": ["SPAM"]}), hist()
    add, rem, _ = o.resolve_target("spam", LABELS)
    r = o.apply_changes(svc, h, "sales", "move to junk", ["m1", "m2", "m3"], add, rem)
    assert [c["id"] for c in r["changed"]] == ["m1", "m2"] and [c["id"] for c in r["already_that_way"]] == ["m3"]
    assert "INBOX" not in svc.labels["m1"] and "SPAM" in svc.labels["m1"] and "UNREAD" in svc.labels["m1"]
    assert len(svc.calls) == 1  # one batch for both
    u = o.undo_last(svc, h, "sales")
    assert u == {"status": "undone", "action": "move to junk", "messages": 2}
    assert svc.labels["m1"] == ["UNREAD", "INBOX"] or set(svc.labels["m1"]) == {"INBOX", "UNREAD"}
    assert svc.labels["m3"] == ["SPAM"]  # untouched
    assert o.undo_last(svc, h, "sales") == {"status": "nothing to undo"}


def test_undo_is_per_account_and_persists():
    path = os.path.join(tempfile.mkdtemp(), "h.json")
    svc = FakeSvc({"m1": ["INBOX"]})
    h = o.History(path)
    o.apply_changes(svc, h, "sales", "archive", ["m1"], [], ["INBOX"])
    assert o.History(path).last("sales") is not None and o.History(path).last("personal") is None
    assert o.undo_last(svc, o.History(path), "sales")["status"] == "undone"
    assert "INBOX" in svc.labels["m1"]


def test_nothing_changed_records_nothing():
    svc, h = FakeSvc({"m1": ["SPAM"]}), hist()
    o.apply_changes(svc, h, "sales", "x", ["m1"], ["SPAM"], ["INBOX"])
    assert h.last("sales") is None and svc.calls == []


class ListSvc(FakeSvc):
    """Adds messages().list with paging, and a minimal get."""

    def list(self, userId, q, maxResults, pageToken=None, includeSpamTrash=False):
        self.queries = getattr(self, "queries", []) + [q]
        want = "UNREAD" if "is:unread" in q else None
        ids = [i for i, l in self.labels.items() if "INBOX" in l and (want is None or want in l)]
        start = int(pageToken or 0)
        page = ids[start:start + min(maxResults, 2)]  # tiny pages, to exercise paging
        nxt = start + len(page)
        return _Exec({"messages": [{"id": i} for i in page], **({"nextPageToken": str(nxt)} if nxt < len(ids) else {})})

    def get(self, userId, id, format, metadataHeaders=None):
        return _Exec({"id": id, "labelIds": list(self.labels[id]), "payload": {"headers": []}})


def test_bulk_mark_read_all_pages_one_undo():
    svc = ListSvc({f"m{i}": ["INBOX", "UNREAD"] for i in range(5)} | {"r": ["INBOX"], "x": ["SENT", "UNREAD"]})
    h = hist()
    r = o.bulk_mark_read(svc, h, "personal")
    assert r["changed"] == 5 and not r["more_remaining"]
    assert all("UNREAD" not in svc.labels[f"m{i}"] for i in range(5))
    assert "UNREAD" in svc.labels["x"]  # outside the inbox: untouched
    assert "is:unread" in svc.queries[0] and "in:inbox" in svc.queries[0]
    assert o.undo_last(svc, h, "personal")["messages"] == 5
    assert all("UNREAD" in svc.labels[f"m{i}"] for i in range(5))


def test_bulk_mark_read_cap_and_nothing_to_do():
    svc = ListSvc({f"m{i}": ["INBOX", "UNREAD"] for i in range(5)})
    r = o.bulk_mark_read(svc, hist(), "p", cap=3)
    assert r["changed"] == 3 and r["more_remaining"]
    assert o.bulk_mark_read(ListSvc({"a": ["INBOX"]}), hist(), "p")["changed"] == 0


def test_bulk_move_needs_a_search_and_is_undoable():
    svc, h = ListSvc({"a": ["INBOX"], "b": ["INBOX", "UNREAD"]}), hist()
    try:
        o.bulk_move(svc, h, "p", "", [], ["INBOX"], "the archive")
        assert False
    except ValueError:
        pass
    r = o.bulk_move(svc, h, "p", "in:inbox", [], ["INBOX"], "the archive")
    assert r["changed"] == 2 and "INBOX" not in svc.labels["a"]
    assert o.undo_last(svc, h, "p")["status"] == "undone" and "INBOX" in svc.labels["a"]


def test_batches_over_a_thousand_are_split():
    svc = ListSvc({f"m{i}": ["INBOX", "UNREAD"] for i in range(1200)})
    o._batch(svc, {((), ("UNREAD",)): list(svc.labels)})
    assert [len(c["ids"]) for c in svc.calls] == [1000, 200]


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"{n} tests passed")
