"""Run with: python3 test_watcher.py   (standard library only; Google libraries are not needed)"""
import os
import tempfile

import watcher as w
import watchlogic as wl


class Resp:
    def __init__(self, status):
        self.status = status


class Gone(Exception):
    def __init__(self):
        self.resp = Resp(404)


class Boom(Exception):
    def __init__(self):
        self.resp = Resp(500)


def fresh_state():
    w.STATE = wl.State(os.path.join(tempfile.mkdtemp(), "watch.json"))
    w.NOTIFIED = []
    w.notify = lambda notice: w.NOTIFIED.append(notice) or True


def test_vanished_message_is_skipped_not_fatal():
    fresh_state()

    def describe(svc, alias, mid):
        if mid == "gone":
            raise Gone()
        return {"account": alias, "message_id": mid, "sender": "Tahir", "verified": True, "subject": "x"}

    w.describe = describe
    w.handle_ids(None, "sales", ["gone", "ok1"])
    assert [n["message_id"] for n in w.NOTIFIED] == ["ok1"]
    assert w.STATE.is_handled("sales", "gone")  # never retried


def test_other_errors_still_raise_so_they_are_retried():
    fresh_state()

    def describe(svc, alias, mid):
        raise Boom()

    w.describe = describe
    try:
        w.handle_ids(None, "sales", ["m1"])
        assert False
    except Boom:
        pass
    assert not w.STATE.is_handled("sales", "m1")


def test_failed_delivery_is_queued_and_not_repeated():
    fresh_state()
    w.describe = lambda svc, alias, mid: {"account": alias, "message_id": mid, "sender": "Fan", "verified": True}
    w.notify = lambda notice: False
    w.handle_ids(None, "sales", ["m1"])
    assert [p["message_id"] for p in w.STATE.pending("sales")] == ["m1"]
    w.handle_ids(None, "sales", ["m1"])  # already handled
    assert len(w.STATE.pending("sales")) == 1


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"{n} tests passed")
