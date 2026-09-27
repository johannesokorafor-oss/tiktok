from tta.textparse import ParsedMetadata, parse_text
from tta.understanding import (
    STYLES,
    classify_style,
    detect_language,
    make_cover_text,
    understand,
)


def _meta(title, desc="", prompt=""):
    return ParsedMetadata(title=title, description=desc, image_prompt=prompt)


def test_language_detection_german():
    assert detect_language("Warum das Universum dir Zeichen schickt und was sie bedeuten") == "de"


def test_language_detection_english():
    assert detect_language("Why the universe sends you signs and what they mean") == "en"


def test_hook_short_title_kept():
    plan = understand(_meta("The universe sends you signs"))
    assert plan.hook == "The universe sends you signs"


def test_hook_long_title_truncated():
    long_title = "This is an extremely long title that keeps going on and on forever"
    plan = understand(_meta(long_title))
    assert len(plan.hook.split()) <= 10
    assert plan.hook.startswith("This is")


def test_cover_text_word_count_and_content():
    plan = understand(_meta(
        "The universe sends you signs",
        "spiritual awakening and energy",
    ))
    words = plan.cover_text.split()
    assert 2 <= len(words) <= 4
    lower = plan.cover_text.lower()
    assert "universe" in lower or "signs" in lower or "sends" in lower


def test_cover_text_german_from_german_material():
    plan = understand(_meta(
        "Das Universum schickt dir Zeichen",
        "Spirituelles Erwachen und Energie für deine Seele",
    ))
    assert plan.language == "de"
    words = plan.cover_text.split()
    assert 2 <= len(words) <= 4
    assert "universum" in plan.cover_text.lower() or "zeichen" in plan.cover_text.lower()


def test_cover_text_not_hardcoded():
    a = understand(_meta("Crypto trading for absolute beginners")).cover_text
    b = understand(_meta("Meditation unlocks your hidden intuition")).cover_text
    assert a != b


def test_style_classification():
    assert classify_style("spiritual awakening universe energy meditation") == "CINEMATIC_MYSTICAL"
    assert classify_style("build wealth money success luxury millionaire") == "DARK_LUXURY"
    assert classify_style("productivity tips and simple app hacks") == "CLEAN_MODERN"
    assert classify_style("completely neutral banana story") == "CLEAN_MODERN"
    assert set(STYLES) == {"CINEMATIC_MYSTICAL", "DARK_LUXURY", "CLEAN_MODERN"}


def test_image_prompt_enhancement_uses_user_prompt():
    plan = understand(_meta("t", "d", "a lone wolf howling at the moon"))
    assert "a lone wolf howling at the moon" in plan.image_prompt
    assert "9:16" in plan.image_prompt
    assert plan.negative_prompt  # non-empty
    assert "watermark" in plan.negative_prompt


def test_image_prompt_derived_when_missing():
    plan = understand(_meta("Morning routine for deep focus",
                            "Simple productivity habits"))
    assert plan.image_prompt
    assert plan.visual_subject


def test_caption_and_hashtags():
    plan = understand(_meta(
        "The universe sends you signs",
        "Learn to trust your intuition. " * 20,
    ))
    assert plan.caption.startswith("The universe sends you signs")
    assert len(plan.caption) <= 2200
    assert plan.hashtags
    assert all(t.startswith("#") for t in plan.hashtags)
    assert any(t != "#fyp" for t in plan.hashtags)  # content-derived tags exist


def test_german_hashtags_ascii_safe():
    plan = understand(_meta("Schöne Träume für deine Seele",
                            "Über Glück und Wünsche"))
    for tag in plan.hashtags:
        assert tag.isascii(), tag


def test_moods_mapped():
    plan = understand(_meta("spiritual universe meditation energy"))
    assert plan.style == "CINEMATIC_MYSTICAL"
    assert plan.visual_mood


def test_cover_text_from_thin_title_uses_keywords():
    plan = understand(parse_text(
        "TITLE: Why?\nDESCRIPTION: discipline builds wealth and success\n"))
    assert 2 <= len(plan.cover_text.split()) <= 4
