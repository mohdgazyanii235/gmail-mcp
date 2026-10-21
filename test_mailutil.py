"""Run with: python3 test_mailutil.py   (standard library only)"""
import email
from email import policy

import mailutil as m

GEMMA = '"Briggs, Gemma (Crown Dependencies – International Private Banking)" <Gemma.Briggs@lloydsbanking.com>'
ME = "mohdgazyanii235@gmail.com"


def parse(raw):
    return email.message_from_bytes(m.b64url_decode(raw), policy=policy.default)


def test_quoted_comma_name_survives():
    raw = m.build_raw_message(GEMMA, "Re: Hello", "Body")
    msg = parse(raw)
    addrs = msg["To"].addresses
    assert len(addrs) == 1, addrs
    assert addrs[0].addr_spec == "Gemma.Briggs@lloydsbanking.com"
    assert "Briggs, Gemma" in addrs[0].display_name


def test_multiple_recipients_and_cc():
    raw = m.build_raw_message(["a@x.com", "B Person <b@x.com>"], "S", "Body", cc="c@x.com; d@x.com", bcc=["e@x.com"])
    msg = parse(raw)
    assert [a.addr_spec for a in msg["To"].addresses] == ["a@x.com", "b@x.com"]
    assert [a.addr_spec for a in msg["Cc"].addresses] == ["c@x.com", "d@x.com"]
    assert [a.addr_spec for a in msg["Bcc"].addresses] == ["e@x.com"]


def test_threading_headers():
    raw = m.build_raw_message("a@x.com", "Re: S", "Body", in_reply_to="<id2@x>", references="<id1@x>")
    msg = parse(raw)
    assert msg["In-Reply-To"] == "<id2@x>"
    assert msg["References"] == "<id1@x> <id2@x>"
    raw = m.build_raw_message("a@x.com", "Re: S", "Body", in_reply_to="<id2@x>")
    assert parse(raw)["References"] == "<id2@x>"


def test_header_injection_blocked():
    for bad in [
        dict(to="a@x.com\nBcc: evil@x.com", subject="S"),
        dict(to="a@x.com", subject="S\r\nBcc: evil@x.com"),
        dict(to=["a@x.com", "b@x.com\nX: y"], subject="S"),
    ]:
        try:
            m.build_raw_message(body="b", **bad)
        except ValueError:
            continue
        raise AssertionError(f"injection not blocked: {bad}")


def test_no_recipient_rejected():
    try:
        m.build_raw_message("", "S", "b")
    except ValueError:
        return
    raise AssertionError("empty recipient accepted")


def test_non_ascii_body_and_subject():
    raw = m.build_raw_message("a@x.com", "Café – test", "Héllo – wörld")
    msg = parse(raw)
    assert msg["Subject"] == "Café – test"
    assert "Héllo – wörld" in msg.get_content()


def test_reply_subject():
    assert m.reply_subject("International Account Enquiry") == "Re: International Account Enquiry"
    assert m.reply_subject("RE: International Account Enquiry") == "RE: International Account Enquiry"
    assert m.reply_subject("re: x") == "re: x"
    assert m.reply_subject("") == "Re:"


def test_reply_recipients():
    h = [
        {"name": "From", "value": GEMMA},
        {"name": "To", "value": f"Mo <{ME}>"},
        {"name": "Cc", "value": "Third <third@x.com>, " + GEMMA},
    ]
    to, cc = m.reply_recipients(h, ME, reply_all=False)
    assert len(to) == 1 and "Gemma.Briggs@lloydsbanking.com" in to[0] and cc == []
    to, cc = m.reply_recipients(h, ME, reply_all=True)
    assert len(to) == 1
    assert len(cc) == 1 and "third@x.com" in cc[0], cc  # not me, not Gemma again
    # Reply-To wins over From
    h2 = [{"name": "From", "value": "a@x.com"}, {"name": "Reply-To", "value": "r@x.com"}]
    to, _ = m.reply_recipients(h2, ME)
    assert to == ["r@x.com"]
    # replying to my own message goes to the original recipients
    h3 = [{"name": "From", "value": ME}, {"name": "To", "value": "g@x.com"}]
    to, _ = m.reply_recipients(h3, ME)
    assert to == ["g@x.com"]


def _enc(s):
    return m.b64url_encode(s.encode())


def test_body_extraction_prefers_plain():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _enc("plain version")}},
            {"mimeType": "text/html", "body": {"data": _enc("<p>html version</p>")}},
        ],
    }
    text, atts = m.extract_body_and_attachments(payload)
    assert text == "plain version" and atts == []


def test_body_extraction_html_fallback_and_attachments():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/html", "body": {"data": _enc("<div>Hello<br>there</div><script>x()</script><p>Bye</p>")}},
            {"mimeType": "application/pdf", "filename": "Data Privacy Notice.pdf", "body": {"attachmentId": "abc"}},
        ],
    }
    text, atts = m.extract_body_and_attachments(payload)
    assert "Hello" in text and "there" in text and "Bye" in text and "x()" not in text
    assert atts == ["Data Privacy Notice.pdf"]


def test_attachment_roundtrip_and_types():
    import base64
    pdf = b"%PDF-1.4 fake bytes \x00\xff" * 50
    item = {"filename": "Invoice March.pdf", "content_base64": base64.b64encode(pdf).decode()}
    files = [m.decode_attachment_input(item), m.decode_attachment_input(
        {"filename": "notes.txt", "content_base64": base64.urlsafe_b64encode(b"hello??>>").decode().rstrip("="), "mime_type": "text/plain"})]
    assert files[0][1] == "application/pdf" and files[0][2] == pdf
    assert files[1][2] == b"hello??>>"
    msg = parse(m.build_raw_message("a@x.com", "S", "Body text", attachments=files))
    assert msg.is_multipart()
    parts = {p.get_filename(): p for p in msg.iter_attachments()}
    assert set(parts) == {"Invoice March.pdf", "notes.txt"}
    assert parts["Invoice March.pdf"].get_content() == pdf
    assert msg.get_body(preferencelist=("plain",)).get_content().strip() == "Body text"


def test_attachment_rejects_bad_input():
    import base64
    ok = base64.b64encode(b"x").decode()
    for bad in [{"filename": "", "content_base64": ok}, {"filename": "../etc/passwd", "content_base64": ok},
                {"filename": "a\r\nBcc: e@x.com", "content_base64": ok}, {"filename": "a.txt", "content_base64": "!!notbase64!!"},
                {"filename": "a.txt", "content_base64": ""}, {"filename": "a.txt"}, "string"]:
        try:
            m.decode_attachment_input(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")
    big = [("big.bin", "application/octet-stream", b"0" * (m.MAX_ATTACHMENT_BYTES + 1))]
    try:
        m.build_raw_message("a@x.com", "S", "B", attachments=big)
        raise AssertionError("oversize accepted")
    except ValueError:
        pass


def test_list_attachment_parts_nested():
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "multipart/alternative", "parts": [{"mimeType": "text/plain", "body": {"data": "SGk"}}]},
        {"mimeType": "application/pdf", "filename": "A.pdf", "body": {"attachmentId": "id1", "size": 2048}},
        {"mimeType": "image/png", "filename": "", "body": {"attachmentId": "inline", "size": 5}}]}
    assert m.list_attachment_parts(payload) == [
        {"filename": "A.pdf", "mime_type": "application/pdf", "size": 2048, "attachment_id": "id1"}]


def test_b64_roundtrip_unpadded():
    for s in [b"", b"a", b"ab", b"abc", b"abcd"]:
        assert m.b64url_decode(m.b64url_encode(s).rstrip("=")) == s


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("ok  ", t.__name__)
    print(f"\n{len(tests)} passed")
