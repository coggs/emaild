from email.message import EmailMessage

from emaild.normalise import chunk_text, clean_body, html_to_text, parse_mime


def _mime(**kw) -> bytes:
    m = EmailMessage()
    m["From"] = kw.get("frm", "Jane Accountant <jane@acct.example>")
    m["To"] = "Jordan <stu@example.com>"
    m["Subject"] = kw.get("subject", "BAS lodgement")
    m["Date"] = "Tue, 06 Oct 2026 09:30:00 +1100"
    m["Message-ID"] = "<abc@acct.example>"
    if "unsub" in kw:
        m["List-Unsubscribe"] = "<https://x.example/u>"
        m["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    m.set_content(kw.get("text", "Hi Jordan,\nThe BAS is due 28 October.\n\nOn Mon, 5 Oct 2026 at 10:00 Jordan wrote:\n> old stuff"))
    if "html" in kw:
        m.add_alternative(kw["html"], subtype="html")
    if kw.get("attach"):
        m.add_attachment(b"%PDF-1.4 data", maintype="application", subtype="pdf", filename="bas.pdf")
    return m.as_bytes()


def test_parse_basic():
    item = parse_mime(_mime())
    assert item.sender.addr == "jane@acct.example"
    assert item.sender.name == "Jane Accountant"
    assert item.subject == "BAS lodgement"
    assert item.sent_at.utcoffset().total_seconds() == 11 * 3600
    assert "due 28 October" in item.body_text
    assert "old stuff" not in item.body_text
    assert "old stuff" in item.full_text


def test_parse_headers_and_attachments():
    item = parse_mime(_mime(unsub=True, attach=True))
    assert item.meta["list_unsubscribe_post"] == "List-Unsubscribe=One-Click"
    assert item.attachments[0].filename == "bas.pdf"
    assert item.attachments[0].mime_type == "application/pdf"


def test_html_only_body():
    m = EmailMessage()
    m["From"] = "a@b.c"
    m["Subject"] = "x"
    m.set_content("<html><body><p>Hello <b>there</b></p><script>bad()</script><div>Line 2</div></body></html>", subtype="html")
    item = parse_mime(m.as_bytes())
    assert "Hello there" in item.body_text
    assert "bad()" not in item.body_text
    assert "Line 2" in item.body_text


def test_clean_outlook_and_signature():
    text = ("Thanks, approved.\n\nCheers\nBob\n-- \nBob Smith | Director\n\n"
            "From: Jordan <s@x.com>\nSent: Monday\nTo: Bob\nSubject: approve?\n\nplease approve")
    out = clean_body(text)
    assert out.startswith("Thanks, approved.")
    assert "please approve" not in out
    assert "Director" not in out


def test_html_to_text_spacing():
    assert html_to_text("<p>a</p><p>b</p>") == "a\nb"


def test_chunking():
    body = " ".join(f"w{i}" for i in range(500))
    chunks = chunk_text("Subj", body, max_words=180, overlap=30)
    assert all(c.startswith("Subject: Subj\n") for c in chunks)
    assert len(chunks) == 4  # starts at 0,150,300,450
    assert "w499" in chunks[-1]
    assert chunk_text("", "") == []
    long = "é" * 10000
    assert len(chunk_text("s", long)[0].encode()) <= 3800
