import os
import zipfile

import pytest

from gateway.platforms import document_extract as dx


def _write_minimal_docx(path: str, paragraphs: list[str]) -> None:
    body = "".join(
        f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("word/document.xml", document_xml)


def test_extract_docx(tmp_path):
    p = str(tmp_path / "cv.docx")
    _write_minimal_docx(p, ["Hello Schmithüsen", "Zeile zwei"])
    text = dx.extract_document_markdown(p, "")
    assert text is not None
    assert "Hello Schmithüsen" in text
    assert "Zeile zwei" in text


def test_extract_docx_by_mime(tmp_path):
    p = str(tmp_path / "noext")
    _write_minimal_docx(p, ["MimeRouted"])
    text = dx.extract_document_markdown(
        p, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert text is not None and "MimeRouted" in text


def test_unsupported_returns_none(tmp_path):
    p = str(tmp_path / "thing.bin")
    with open(p, "wb") as f:
        f.write(b"\x00\x01\x02")
    assert dx.extract_document_markdown(p, "application/octet-stream") is None


def test_corrupt_docx_returns_none(tmp_path):
    p = str(tmp_path / "broken.docx")
    with open(p, "wb") as f:
        f.write(b"not a zip at all")
    assert dx.extract_document_markdown(p, "") is None  # never raises


def test_char_cap(tmp_path):
    p = str(tmp_path / "big.docx")
    _write_minimal_docx(p, ["A" * 500_000])
    text = dx.extract_document_markdown(p, "")
    assert text is not None
    assert len(text) <= dx.MAX_EXTRACT_CHARS + 64  # cap + truncation marker


def test_write_sidecar(tmp_path):
    original = str(tmp_path / "doc_abc123_cv.docx")
    with open(original, "wb") as f:
        f.write(b"orig")
    sidecar = dx.write_markdown_sidecar(original, "# Hello\n")
    assert sidecar == original + ".md"
    assert os.path.exists(sidecar)
    with open(sidecar, encoding="utf-8") as f:
        assert f.read() == "# Hello\n"


def test_extract_pdf(tmp_path):
    fitz = pytest.importorskip("fitz")  # pymupdf — present in gateway venv
    p = str(tmp_path / "note.pdf")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Hello PDF Schmithüsen")
    doc.save(p)
    doc.close()
    text = dx.extract_document_markdown(p, "application/pdf")
    assert text is not None
    assert "Hello PDF" in text
