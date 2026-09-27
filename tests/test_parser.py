import pytest

from app.parsing.text_parser import ParseError, parse_file, parse_text


def test_canonical_format():
    m = parse_text("TITLE:\nMy Title\n\nDESCRIPTION:\nSome text here.\n\nIMAGE_PROMPT:\nA dark sky.")
    assert m.title == "My Title"
    assert m.description == "Some text here."
    assert m.image_prompt == "A dark sky."
    assert not m.used_fallback


def test_flexible_headings_case_insensitive_and_german():
    m = parse_text("titel: Mein Titel\nBeschreibung: Ein Text über Intuition\nBildprompt: Nachthimmel")
    assert m.title == "Mein Titel"
    assert m.description.startswith("Ein Text")
    assert m.image_prompt == "Nachthimmel"


def test_markdown_headings_and_prompt_alias():
    m = parse_text("## Title\nHello\n\n## Content\nBody text\n\n## Prompt\nVisual idea")
    assert m.title == "Hello"
    assert m.description == "Body text"
    assert m.image_prompt == "Visual idea"


def test_image_prompt_wins_over_prompt_key_ordering():
    m = parse_text("TITLE: T\nDESCRIPTION: D\nIMAGE PROMPT: the visual")
    assert m.image_prompt == "the visual"


def test_fallback_natural_text():
    m = parse_text("Warum dein Bauchgefühl recht hat\n\nEine Reflexion über Intuition. Und mehr.")
    assert m.used_fallback
    assert m.title == "Warum dein Bauchgefühl recht hat"
    assert "Intuition" in m.description
    assert m.image_prompt == ""


def test_missing_title_derived_from_description():
    m = parse_text("DESCRIPTION:\nErster Satz hier. Zweiter Satz.")
    assert m.title == "Erster Satz hier."
    assert m.used_fallback


def test_hashtags_extracted():
    m = parse_text("TITLE: T\nDESCRIPTION: text #Seele #intuition here")
    assert [h.lower() for h in m.hashtags] == ["seele", "intuition"]


def test_empty_file_rejected():
    with pytest.raises(ParseError):
        parse_text("   \n\n ")
    with pytest.raises(ParseError):
        parse_text("")


def test_parse_file_preserves_source(tmp_path, metadata_text):
    p = tmp_path / "m.txt"
    p.write_text(metadata_text, encoding="utf-8")
    before = p.read_bytes()
    m = parse_file(p, video_file="v.mp4")
    assert m.video_file == "v.mp4"
    assert p.read_bytes() == before  # untouched


def test_umlauts_and_bom(tmp_path):
    p = tmp_path / "bom.txt"
    p.write_text("TITLE:\nGrüße über Öl\n\nDESCRIPTION:\nStraße", encoding="utf-8-sig")
    m = parse_file(p)
    assert m.title == "Grüße über Öl"


def test_cover_text_and_style_overrides():
    m = parse_text("TITLE: T\nDESCRIPTION: D\nCOVER_TEXT: DU ERKENNST ES\nSTYLE: dark_luxury")
    assert m.cover_text == "DU ERKENNST ES"
    assert m.style == "DARK_LUXURY"
