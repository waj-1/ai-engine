import io
import docx
from admin.kb_handlers import _extract_text


def _make_docx(paragraphs):
    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_extract_text_reads_docx_paragraphs():
    raw = _make_docx(["Hello Orchard", "Second line"])
    text = _extract_text("notes.docx", raw)
    assert "Hello Orchard" in text and "Second line" in text


def test_extract_text_still_handles_txt():
    assert _extract_text("a.txt", b"plain body") == "plain body"
