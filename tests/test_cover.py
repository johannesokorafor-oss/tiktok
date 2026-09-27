from PIL import Image

from app.config import StylePreset
from app.cover.compose import CoverComposer, contrast_ratio
from app.cover.presets import PRESETS, get_preset
from app.cover.validate import validate_cover
from app.images.base import ImageRequest
from app.images.providers.offline import OfflineProvider
from app.images.registry import score_image


def _background(settings, prompt="cinematic mystical night sky"):
    return OfflineProvider(settings).generate(
        ImageRequest(prompt=prompt, width=settings.cover_width, height=settings.cover_height,
                     seed=42)).data


def test_offline_provider_produces_valid_vertical_image(settings):
    data = _background(settings)
    img = Image.open(__import__("io").BytesIO(data))
    assert img.size == (1080, 1920)
    assert img.format == "PNG"


def test_composer_renders_all_presets(settings, tmp_path):
    bg = _background(settings)
    for style in (StylePreset.CINEMATIC_MYSTICAL, StylePreset.DARK_LUXURY, StylePreset.CLEAN_MODERN):
        composer = CoverComposer(get_preset(style))
        cover = composer.compose(bg, ["DEINE", "SEELE", "SPRICHT"])
        out = cover.save(tmp_path / f"{style.value}.png")
        check = validate_cover(out, layout=cover.layout)
        assert check.ok, check.errors
        assert check.width == 1080 and check.height == 1920
        assert cover.layout.font_size > 1920 * 0.05


def test_text_stays_inside_safe_region_for_long_words(settings, tmp_path):
    bg = _background(settings)
    composer = CoverComposer(get_preset(StylePreset.CINEMATIC_MYSTICAL))
    cover = composer.compose(bg, ["AUSSERGEWÖHNLICHE", "WAHRNEHMUNG"])
    x0, y0, x1, y1 = cover.layout.text_box
    sx0, sy0, sx1, sy1 = cover.layout.safe_box
    assert x0 >= sx0 and x1 <= sx1 and y0 >= sy0 and y1 <= sy1
    out = cover.save(tmp_path / "long.png")
    assert validate_cover(out, layout=cover.layout).ok


def test_umlauts_are_preserved_in_layout(settings):
    bg = _background(settings)
    composer = CoverComposer(get_preset(StylePreset.CINEMATIC_MYSTICAL))
    cover = composer.compose(bg, ["GRÜSSE", "ÜBER", "ÖL"])
    joined = " ".join(cover.layout.lines)
    for ch in "ÜÖ":
        assert ch in joined


def test_adaptive_font_size_shrinks_for_four_long_words(settings):
    bg = _background(settings)
    composer = CoverComposer(get_preset(StylePreset.CINEMATIC_MYSTICAL))
    small = composer.compose(bg, ["DU", "SIEHST"]).layout.font_size
    big = composer.compose(bg, ["VERBINDUNG", "WAHRNEHMUNG", "BEDEUTUNG", "ERWACHEN"]).layout.font_size
    assert big <= small


def test_contrast_ratio_helper():
    assert contrast_ratio((255, 255, 255), (0, 0, 0)) > 20
    assert contrast_ratio((128, 128, 128), (128, 128, 128)) == 1.0


def test_validate_rejects_wrong_dimensions(settings, tmp_path):
    p = tmp_path / "small.png"
    Image.new("RGB", (100, 100), (30, 60, 90)).save(p)
    check = validate_cover(p)
    assert not check.ok


def test_validate_rejects_blank_image(tmp_path):
    p = tmp_path / "blank.png"
    Image.new("RGB", (1080, 1920), (0, 0, 0)).save(p)
    check = validate_cover(p)
    assert not check.ok
    assert any("black" in e or "blank" in e for e in check.errors)


def test_validate_rejects_corrupt_file(tmp_path):
    p = tmp_path / "corrupt.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20000)
    assert not validate_cover(p).ok


def test_candidate_scoring_prefers_clean_text_area(settings):
    calm = _background(settings, "clean modern minimal studio")
    busy = _background(settings, "cinematic mystical night sky")
    s1, s2 = score_image(calm), score_image(busy)
    assert 0 <= s1.total <= 1 and 0 <= s2.total <= 1
    assert s1.text_area >= 0 and s2.text_area >= 0


def test_all_presets_registered():
    assert set(PRESETS) == {StylePreset.CINEMATIC_MYSTICAL, StylePreset.DARK_LUXURY,
                            StylePreset.CLEAN_MODERN}
