"""Run with: python3 test_watchlogic.py   (standard library only)"""
import base64
import json
import os
import tempfile

import watchlogic as w

S = w.DEFAULT_SENDERS


def test_parse_push_base64():
    data = base64.b64encode(json.dumps({"emailAddress": "Sales@Sanyang.co.uk", "historyId": 123}).encode())
    assert w.parse_push(data) == ("sales@sanyang.co.uk", 123)


def test_parse_push_plain_json_and_bad():
    assert w.parse_push(b'{"emailAddress":"a@b.com","historyId":"7"}') == ("a@b.com", 7)
    try:
        w.parse_push(b'{"historyId":1}')
        assert False
    except ValueError:
        pass


def test_sender_exact_address_only():
    assert w.sender_name('"Tahir Mahmood" <Tahir@SKGGroup.co.uk>', S) == "Tahir"
    assert w.sender_name("gltrd@eim.ae", S) == "your dad"
    # Display name says Tahir but the address is someone else: no match.
    assert w.sender_name('"Tahir Mahmood" <evil@example.com>', S) is None
    # Look-alike domains do not match.
    assert w.sender_name("tahir@skggroup.co.uk.evil.com", S) is None
    assert w.sender_name("tahir@skggroup.com", S) is None
    assert w.sender_name("", S) is None


def test_auth_dmarc_pass():
    ar = ["mx.google.com; dkim=pass header.i=@skggroup.co.uk header.s=x; spf=pass; dmarc=pass (p=NONE) header.from=skggroup.co.uk"]
    assert w.auth_verified(ar, "tahir@skggroup.co.uk")


def test_auth_dkim_only_must_match_domain():
    good = ["mx.google.com; dkim=pass header.i=@eim.ae header.d=eim.ae; spf=none"]
    assert w.auth_verified(good, "gltrd@eim.ae")
    other = ["mx.google.com; dkim=pass header.i=@evil.com header.d=evil.com; spf=pass"]
    assert not w.auth_verified(other, "gltrd@eim.ae")


def test_auth_fail_or_missing():
    assert not w.auth_verified([], "a@b.com")
    assert not w.auth_verified(["mx.google.com; dkim=fail; spf=fail; dmarc=fail"], "a@b.com")
    assert not w.auth_verified(["mx; dmarc=pass"], "")


def test_auth_only_first_header_counts():
    forged = ["mx.google.com; dmarc=fail", "attacker.example; dmarc=pass"]
    assert not w.auth_verified(forged, "a@b.com")


def test_new_inbox_ids():
    h = {"history": [
        {"messagesAdded": [{"message": {"id": "1", "labelIds": ["INBOX", "UNREAD"]}}]},
        {"messagesAdded": [{"message": {"id": "2", "labelIds": ["SENT"]}}]},
        {"messagesAdded": [{"message": {"id": "1", "labelIds": ["INBOX"]}}, {"message": {"id": "3", "labelIds": ["INBOX"]}}]},
    ]}
    assert w.new_inbox_ids(h) == ["1", "3"]
    assert w.new_inbox_ids({}) == []


def test_state_roundtrip_and_pending():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "s", "watch.json")
        st = w.State(p)
        assert not st.known("sales")
        st.set_history_id("sales", 10)
        st.set_history_id("sales", 5)  # never goes backwards
        st.mark_handled("sales", "m1")
        st.add_pending("sales", {"message_id": "m2", "x": 1})
        st.add_pending("sales", {"message_id": "m2", "x": 1})
        st.save()
        st2 = w.State(p)
        assert st2.known("sales") and st2.history_id("sales") == 10
        assert st2.is_handled("sales", "m1") and not st2.is_handled("sales", "m9")
        assert len(st2.pending("sales")) == 1
        st2.clear_pending("sales", "m2")
        assert st2.pending("sales") == []


def test_handled_is_capped():
    with tempfile.TemporaryDirectory() as d:
        st = w.State(os.path.join(d, "w.json"))
        for i in range(w.MAX_HANDLED + 50):
            st.mark_handled("a", str(i))
        assert len(st._acc("a")["handled"]) == w.MAX_HANDLED
        assert st.is_handled("a", str(w.MAX_HANDLED + 49)) and not st.is_handled("a", "0")


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
    print(f"{n} tests passed")
