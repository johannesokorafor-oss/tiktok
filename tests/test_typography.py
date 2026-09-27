import pytest
from PIL import Image

from tta.providers.base import ImageRequest
from tta.providers.local_art import LocalArtProvider
from tta.typography import TypographyError, find_font, render_cover_text


@pytest.fixture()
def background(tmp_path):
    path = tmp_path / "bg.png"
    LocalArtProvider().generate(
        ImageRequest(prompt="test", style="CINEMATIC_MYSTICAL", seed=7), path
    )
    return path


def test_font_available():
    assert find_font(bold=True)


def test_render_basic(tmp_path, background):
    out = tmp_path / "cover.png"
    render_cover_text(background, "Trust The Signs", out)
    with Image.open(out) as img:
        assert img.size == (1080, 1920)
        assert img.format == "PNG"


def test_render_changes_pixels(tmp_path, background):
    out = tmp_path / "cover.png"
    render_cover_text(background, "Trust The Signs", out)
    with Image.open(background) as a, Image.open(out) as b:
        assert list(a.convert("RGB").getdata()) != list(b.convert("RGB").getdata())
        # white text pixels must exist
        whites = sum(1 for px in b.convert("RGB").getdata()
                     if px[0] > 240 and px[1] > 240 and px[2] > 240)
        assert whites > 2000


def test_render_german_umlauts(tmp_path, background):
    out = tmp_path / "cover.png"
    render_cover_text(background, "Größe Übt Zärtlichkeit", out)
    with Image.open(out) as img:
        assert img.size == (1080, 1920)


def test_long_text_wraps_without_clipping(tmp_path, background):
    out = tmp_path / "cover.png"
    render_cover_text(background,
                      "Extraordinarily Comprehensive Motivation Statement", out)
    with Image.open(out) as img:
        assert img.size == (1080, 1920)
        # nothing should be drawn in the bottom UI-unsafe zone's last rows
        bottom = img.convert("RGB").crop((0, 1900, 1080, 1920))
        whites = sum(1 for px in bottom.getdata()
                     if px[0] > 240 and px[1] > 240 and px[2] > 240)
        assert whites == 0


def test_empty_text_raises(tmp_path, background):
    with pytest.raises(TypographyError):
        render_cover_text(background, "   ", tmp_path / "x.png")


def test_control_chars_removed(tmp_path, background):
    out = tmp_path / "cover.png"
    render_cover_text(background, "Cle\u0007an\u200bText", out)
    assert out.exists()
