import os

import gateway.run as run


def test_wiring_success_inlines_and_writes_sidecar(tmp_path, monkeypatch):
    original = str(tmp_path / "doc_abc123_cv.docx")
    with open(original, "wb") as f:
        f.write(b"orig-bytes")

    import gateway.platforms.document_extract as dx
    monkeypatch.setattr(dx, "extract_document_markdown",
                        lambda p, m: "# Werkliste\n- Stück A\n- Stück B")

    note, inline = run._document_attachment_context(
        path=original,
        agent_path="/root/.hermes/cache/documents/doc_abc123_cv.docx",
        display_name="cv.docx",
        mtype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert inline == "# Werkliste\n- Stück A\n- Stück B"
    assert "cv.docx" in note
    assert os.path.exists(original + ".md")          # sidecar written
    with open(original + ".md", encoding="utf-8") as f:
        assert "Werkliste" in f.read()


def test_wiring_failure_falls_back(tmp_path, monkeypatch):
    original = str(tmp_path / "doc_def456_scan.pdf")
    with open(original, "wb") as f:
        f.write(b"not extractable")

    import gateway.platforms.document_extract as dx
    monkeypatch.setattr(dx, "extract_document_markdown", lambda p, m: None)

    note, inline = run._document_attachment_context(
        path=original,
        agent_path="/root/.hermes/cache/documents/doc_def456_scan.pdf",
        display_name="scan.pdf",
        mtype="application/pdf",
    )
    assert inline is None
    # legacy note wording preserved on the fallback path
    assert note == run._build_document_context_note(
        "scan.pdf", "/root/.hermes/cache/documents/doc_def456_scan.pdf", "application/pdf"
    )
    assert not os.path.exists(original + ".md")       # no sidecar on failure


def test_wiring_text_mime_uses_legacy(tmp_path):
    note, inline = run._document_attachment_context(
        path=str(tmp_path / "doc_x_readme.txt"),
        agent_path="/root/.hermes/cache/documents/doc_x_readme.txt",
        display_name="readme.txt",
        mtype="text/plain",
    )
    assert inline is None
    assert "readme.txt" in note
