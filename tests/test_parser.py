from pathlib import Path

from tta.textparse import parse_file, parse_text


def test_uppercase_block_format():
    meta = parse_text(
        "TITLE:\nMy Video\n\nDESCRIPTION:\nA longer text\nover two lines\n\n"
        "IMAGE_PROMPT:\na misty forest at dawn\n"
    )
    assert meta.title == "My Video"
    assert "longer text" in meta.description
    assert "two lines" in meta.description
    assert meta.image_prompt == "a misty forest at dawn"


def test_inline_mixed_case_and_prompt_alias():
    meta = parse_text(
        "Title: Hello World\nDescription: something nice\nPrompt: golden sunset\n"
    )
    assert meta.title == "Hello World"
    assert meta.description == "something nice"
    assert meta.image_prompt == "golden sunset"


def test_case_insensitive_and_equals_separator():
    meta = parse_text("tItLe = Spaces ok\ndESCRIPTION= yes\nIMAGE PROMPT= a cat\n")
    assert meta.title == "Spaces ok"
    assert meta.description == "yes"
    assert meta.image_prompt == "a cat"


def test_markdown_headings_and_bold():
    meta = parse_text(
        "## Title: Markdown style\n**Description:** bold text\n### Prompt: neon city\n"
    )
    assert meta.title == "Markdown style"
    assert meta.description == "bold text"
    assert meta.image_prompt == "neon city"


def test_german_aliases_and_umlauts():
    meta = parse_text(
        "Titel: Größe zählt\nBeschreibung: Über Wünsche und Träume\n"
        "Bildprompt: ätherisches Licht\n"
    )
    assert meta.title == "Größe zählt"
    assert "Wünsche" in meta.description
    assert meta.image_prompt == "ätherisches Licht"


def test_fallback_without_keys():
    meta = parse_text("Just a plain title line\nAnd this becomes\nthe description.\n")
    assert meta.title == "Just a plain title line"
    assert "description" in meta.description


def test_crlf_and_bom():
    meta = parse_text("\ufeffTITLE: Windows file\r\nDESCRIPTION: crlf\r\n")
    assert meta.title == "Windows file"
    assert meta.description == "crlf"


def test_empty_input():
    meta = parse_text("")
    assert meta.title == "" and meta.description == "" and meta.image_prompt == ""


def test_unknown_keys_stay_in_active_section():
    meta = parse_text("TITLE: t\nDESCRIPTION:\nline one\nNote: still description\n")
    assert "still description" in meta.description


def test_file_is_not_modified(tmp_path: Path):
    f = tmp_path / "meta.txt"
    original = "TITLE: Untouched\nDESCRIPTION: keep me\n"
    f.write_text(original, encoding="utf-8")
    before = f.read_bytes()
    meta = parse_file(f)
    assert meta.title == "Untouched"
    assert f.read_bytes() == before


def test_cp1252_fallback(tmp_path: Path):
    f = tmp_path / "meta.txt"
    f.write_bytes("TITLE: Größe\n".encode("cp1252"))
    meta = parse_file(f)
    assert "Gr" in meta.title
