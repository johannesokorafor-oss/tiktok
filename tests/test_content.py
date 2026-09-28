from app.content.analyzer import (analyze, build_caption, build_hashtags, classify_topic,
                                  detect_language)
from app.parsing.text_parser import parse_text


def test_language_detection_german_and_english():
    assert detect_language("Die Zeichen, die deine Seele dir zeigen will") == "de"
    assert detect_language("The signs your soul is trying to show you") == "en"
    assert detect_language("12345", default="de") == "de"






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




def test_plan_is_deterministic(metadata_text):
    a = analyze(parse_text(metadata_text)).to_dict()
    b = analyze(parse_text(metadata_text)).to_dict()
    assert a == b

def test_image_prompt_is_preserved_but_never_used(metadata_text):
    """IMAGE_PROMPT stays in the metadata and triggers no image work."""
    plan = analyze(parse_text(metadata_text))
    assert plan.image_prompt.startswith("Cinematic mystical night scene")
    assert not hasattr(plan, "cover_text")
    assert not hasattr(plan, "hook_words")
    assert not hasattr(plan, "style_preset")
    assert not hasattr(plan, "negative_prompt")


def test_plan_contains_only_text_fields(metadata_text):
    keys = set(analyze(parse_text(metadata_text)).to_dict())
    assert keys == {"language", "tiktok_title", "caption", "hashtags", "topic",
                    "keywords", "image_prompt"}


def test_missing_image_prompt_is_fine():
    plan = analyze(parse_text("TITLE: T\nDESCRIPTION: Eine kurze Beschreibung über Intuition."))
    assert plan.image_prompt == ""
    assert plan.caption and plan.hashtags


def test_title_and_caption_overrides():
    plan = analyze(parse_text("TITLE: T\nDESCRIPTION: D"),
                   caption_override="my caption", title_override="my title")
    assert plan.caption == "my caption" and plan.tiktok_title == "my title"


def test_topic_classification_still_works(metadata_text):
    assert classify_topic(parse_text(metadata_text)) == "spiritual"
