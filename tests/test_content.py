from app.config import StylePreset
from app.content.analyzer import (analyze, build_caption, build_hashtags, derive_hook,
                                  detect_language)
from app.parsing.text_parser import parse_text


def test_language_detection_german_and_english():
    assert detect_language("Die Zeichen, die deine Seele dir zeigen will") == "de"
    assert detect_language("The signs your soul is trying to show you") == "en"
    assert detect_language("12345", default="de") == "de"


def test_hook_is_short_and_content_derived(metadata_text):
    meta = parse_text(metadata_text)
    hook = derive_hook(meta, "de")
    assert 2 <= len(hook) <= 4
    assert all(w == w.upper() for w in hook)
    text = (meta.title + " " + meta.description).lower()
    assert all(w.lower() in text for w in hook)


def test_hook_is_not_the_whole_title(metadata_text):
    meta = parse_text(metadata_text)
    hook = " ".join(derive_hook(meta, "de"))
    assert hook.lower() != meta.title.lower()
    assert len(hook) < len(meta.title)


def test_hook_override_respected():
    meta = parse_text("TITLE: T\nDESCRIPTION: Something about intuition")
    plan = analyze(meta, hook_override="deine seele spricht")
    assert plan.hook_words == ["DEINE", "SEELE", "SPRICHT"]


def test_cover_text_field_used_as_hook():
    meta = parse_text("TITLE: T\nDESCRIPTION: D\nCOVER_TEXT: NICHT ALLES ZUFALL")
    assert derive_hook(meta, "de") == ["NICHT", "ALLES", "ZUFALL"]


def test_caption_respects_limit_and_appends_hashtags():
    meta = parse_text("TITLE: T\nDESCRIPTION: " + ("wort " * 900))
    caption = build_caption(meta, "de", ["intuition", "seele"], 2200)
    assert len(caption) <= 2200
    assert caption.endswith("#intuition #seele")


def test_hashtags_are_few_and_relevant(metadata_text):
    meta = parse_text(metadata_text)
    tags = build_hashtags(meta, "de", "spiritual")
    assert 1 <= len(tags) <= 5
    assert len(set(tags)) == len(tags)


def test_style_auto_selection(metadata_text):
    plan = analyze(parse_text(metadata_text))
    assert plan.topic == "spiritual"
    assert plan.style_preset == StylePreset.CINEMATIC_MYSTICAL
    plan2 = analyze(parse_text("TITLE: KI Tutorial\nDESCRIPTION: software code guide science"))
    assert plan2.style_preset == StylePreset.CLEAN_MODERN


def test_prompt_contains_engineering_directives(metadata_text):
    plan = analyze(parse_text(metadata_text))
    p = plan.image_prompt.lower()
    for token in ("9:16", "lighting", "composition", "no text", "negative space"):
        assert token in p
    for token in ("text", "watermark", "extra limbs", "logo"):
        assert token in plan.negative_prompt.lower()


def test_plan_is_deterministic(metadata_text):
    a = analyze(parse_text(metadata_text)).to_dict()
    b = analyze(parse_text(metadata_text)).to_dict()
    assert a == b
