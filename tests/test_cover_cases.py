"""Cover-text / hook quality cases (A-F from the QA checklist)."""

import re

from PIL import Image

from tta.textparse import ParsedMetadata
from tta.typography import render_cover_text
from tta.understanding import understand


def _meta(title, desc="", prompt=""):
    return ParsedMetadata(title=title, description=desc, image_prompt=prompt)


# CASE A: title already contains >= 3 strong words -> use them, no filler
def test_case_a_english_strong_title_no_filler():
    plan = understand(_meta("Discipline Builds Real Wealth",
                            "Some unrelated filler description text here"))
    words = plan.cover_text.split()
    assert 2 <= len(words) <= 4
    allowed = {"discipline", "builds", "real", "wealth"}
    assert all(w.lower() in allowed for w in words), plan.cover_text


def test_case_a_german_strong_title_no_declined_possessive_filler():
    plan = understand(_meta("Vertraue deiner inneren Stimme",
                            "Es geht um Achtsamkeit und Ruhe im Alltag"))
    words = [w.lower() for w in plan.cover_text.split()]
    assert 2 <= len(words) <= 4
    assert "deiner" not in words          # declined possessive is filler
    assert all(w in {"vertraue", "inneren", "stimme"} for w in words), plan.cover_text


# CASE A+: German 4-word title with adjective -> keep the noun object,
# never end on a dangling adjective ("Disziplin Schafft Echten")
def test_case_a_german_noun_preference_no_dangling_adjective():
    plan = understand(_meta("Disziplin schafft echten Wohlstand",
                            "Warum Investoren auf Disziplin setzen."))
    words = [w.lower() for w in plan.cover_text.split()]
    assert 2 <= len(words) <= 4
    assert "wohlstand" in words, plan.cover_text     # object noun kept
    assert words[-1] != "echten", plan.cover_text    # no dangling adjective


# CASE B: only 1 meaningful title word -> top up from content keywords
def test_case_b_thin_title_topped_up_from_keywords():
    plan = understand(_meta("Warum?",
                            "Disziplin schafft Wohlstand und langfristigen Erfolg"))
    words = [w.lower() for w in plan.cover_text.split()]
    assert 2 <= len(words) <= 4
    content = {"disziplin", "schafft", "wohlstand", "langfristigen", "erfolg", "warum"}
    assert any(w in content for w in words), plan.cover_text


# CASE C: long German title -> concise hook, not the whole title
def test_case_c_long_german_title_concise_hook():
    title = ("Warum das Universum dir immer genau dann besondere Zeichen "
             "schickt wenn du sie am allerwenigsten erwartest")
    plan = understand(_meta(title, "Spirituelle Impulse für deinen Alltag"))
    assert plan.language == "de"
    assert len(plan.hook.split()) <= 10
    assert len(plan.hook) < len(title)
    assert plan.hook.endswith("…")
    words = plan.cover_text.split()
    assert 2 <= len(words) <= 4


# CASE D: English material -> English cover text
def test_case_d_english_material_english_cover():
    plan = understand(_meta("Trust the signs around you",
                            "The universe always answers when you listen"))
    assert plan.language == "en"
    for w in plan.cover_text.split():
        assert w.isascii(), plan.cover_text
    assert any(w.lower() in {"trust", "signs", "around"} for w in plan.cover_text.split())


# CASE E: umlauts survive understanding AND rendering
def test_case_e_umlauts_preserved_in_plan():
    plan = understand(_meta("Schöne Träume stärken deine Seele",
                            "Über Glück, Wünsche und größere Ziele"))
    assert plan.language == "de"
    joined = plan.cover_text
    # meaningful title words with umlauts are kept intact, not mangled
    assert "Schöne" in joined or "Träume" in joined, joined
    assert "?" not in joined and "\ufffd" not in joined


def test_case_e_umlauts_render_correctly(tmp_path):
    from tta.providers.base import ImageRequest
    from tta.providers.local_art import LocalArtProvider

    bg = tmp_path / "bg.png"
    LocalArtProvider().generate(ImageRequest(prompt="x", seed=11,
                                             style="CINEMATIC_MYSTICAL"), bg)
    out = tmp_path / "cover.png"
    render_cover_text(bg, "Größere Träume Über ßÄÖÜ", out)
    with Image.open(out) as img:
        assert img.size == (1080, 1920)
        whites = sum(
            1 for px in img.convert("RGB").getdata()
            if px[0] > 240 and px[1] > 240 and px[2] > 240
        )
        assert whites > 2000  # glyphs actually rendered (no tofu blanks)


# CASE F: punctuation / numbers / odd capitalization -> clean output
def test_case_f_punctuation_and_numbers_cleaned():
    plan = understand(_meta("5 KRASSE Geld-Hacks!!!",
                            "Sofort umsetzbare Tipps für mehr Geld im Alltag"))
    assert not re.search(r"[!?]{2,}$", plan.hook), plan.hook
    words = plan.cover_text.split()
    assert 2 <= len(words) <= 4
    for w in words:
        assert re.fullmatch(r"[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß'\-]*", w), plan.cover_text


def test_case_f_hook_single_terminal_punctuation():
    plan = understand(_meta("Is this real???"))
    assert plan.hook.endswith("?") and not plan.hook.endswith("??")


# No hard-coded phrases: different content -> different derived text
def test_cover_and_hook_derived_not_hardcoded():
    plans = [
        understand(_meta("Morning routines that changed my life")),
        understand(_meta("Das Geheimnis erfolgreicher Investoren")),
        understand(_meta("Quantum computers explained simply")),
    ]
    covers = {p.cover_text for p in plans}
    hooks = {p.hook for p in plans}
    assert len(covers) == 3 and len(hooks) == 3
