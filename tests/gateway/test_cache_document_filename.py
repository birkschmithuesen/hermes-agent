import os
import unicodedata

from gateway.platforms import base


def test_cached_filename_is_nfc(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "get_document_cache_dir", lambda: tmp_path)

    nfd_name = unicodedata.normalize("NFD", "CV-Birk_Schmithüsen_DE.docx")
    nfc_name = unicodedata.normalize("NFC", "CV-Birk_Schmithüsen_DE.docx")
    assert nfd_name != nfc_name  # sanity: the input really is decomposed

    path = base.cache_document_from_bytes(b"payload", nfd_name)

    assert os.path.exists(path)
    basename = os.path.basename(path)
    # cached name is doc_<uuid12>_<original>; recover the original portion
    original_portion = basename.split("_", 2)[2]
    assert original_portion == unicodedata.normalize("NFC", original_portion)
    assert original_portion == nfc_name
    # precomposed byte marker: NFC ü is a single code point
    assert "̈" not in original_portion  # no combining diaeresis left


def test_cache_document_none_filename(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "get_document_cache_dir", lambda: tmp_path)
    path = base.cache_document_from_bytes(b"x", "")
    assert os.path.exists(path)
    assert os.path.basename(path).split("_", 2)[2] == "document"
