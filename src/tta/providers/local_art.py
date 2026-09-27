"""Local procedural background renderer (CostClass.LOCAL).

Generates premium, cinematic 9:16 backgrounds fully offline with Pillow.
Deterministic for a given prompt/seed.  This provider is always available
and is the guaranteed fallback, so the pipeline works end-to-end without
any network or GPU.
"""

from __future__ import annotations

import hashlib
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from .base import CostClass, ImageProvider, ImageRequest, ProviderError


def _seed_from(request: ImageRequest) -> int:
    if request.seed is not None:
        return request.seed
    digest = hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _lerp(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _vertical_gradient(size: tuple[int, int], stops: list[tuple[float, tuple]]) -> Image.Image:
    """Multi-stop vertical gradient."""
    width, height = size
    img = Image.new("RGB", (1, height))
    px = img.load()
    for y in range(height):
        t = y / max(1, height - 1)
        # find surrounding stops
        prev_pos, prev_col = stops[0]
        next_pos, next_col = stops[-1]
        for pos, col in stops:
            if pos <= t:
                prev_pos, prev_col = pos, col
            if pos >= t:
                next_pos, next_col = pos, col
                break
        local = 0.0 if next_pos == prev_pos else (t - prev_pos) / (next_pos - prev_pos)
        px[0, y] = _lerp(prev_col, next_col, local)
    return img.resize((width, height))


def _radial_glow(size, center, radius, color, peak_alpha) -> Image.Image:
    """RGBA layer with a soft radial glow."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    steps = 40
    for i in range(steps, 0, -1):
        r = radius * i / steps
        alpha = int(peak_alpha * (1 - i / steps) ** 2)
        draw.ellipse(
            [center[0] - r, center[1] - r, center[0] + r, center[1] + r],
            fill=color + (alpha,),
        )
    return layer.filter(ImageFilter.GaussianBlur(radius / 12))


def _vignette(img: Image.Image, strength: float = 0.55) -> Image.Image:
    width, height = img.size
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse(
        [-width * 0.35, -height * 0.25, width * 1.35, height * 1.25], fill=255
    )
    mask = mask.filter(ImageFilter.GaussianBlur(width // 4))
    black = Image.new("RGB", (width, height), (0, 0, 0))
    inverted = mask.point(lambda v: int((255 - v) * strength))
    return Image.composite(black, img, inverted)


def _grain(img: Image.Image, rng: random.Random, amount: int = 10) -> Image.Image:
    small_w, small_h = img.width // 3, img.height // 3
    noise = Image.new("L", (small_w, small_h))
    noise.putdata([rng.randint(128 - amount, 128 + amount) for _ in range(small_w * small_h)])
    noise = noise.resize(img.size).convert("RGB")
    return Image.blend(img, Image.blend(img, noise, 0.5), 0.12)


def _light_rays(size, origin, rng: random.Random, color, count=6, alpha=26) -> Image.Image:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for _ in range(count):
        angle = math.radians(rng.uniform(55, 125))
        spread = math.radians(rng.uniform(2, 6))
        length = size[1] * rng.uniform(0.7, 1.3)
        x0, y0 = origin
        p1 = (x0 + length * math.cos(angle - spread), y0 + length * math.sin(angle - spread))
        p2 = (x0 + length * math.cos(angle + spread), y0 + length * math.sin(angle + spread))
        draw.polygon([origin, p1, p2], fill=color + (alpha,))
    return layer.filter(ImageFilter.GaussianBlur(18))


def _mountains(size, rng: random.Random, base_y: float, color, jag=0.06) -> Image.Image:
    width, height = size
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    points = [(0, height)]
    y = base_y * height
    x = 0.0
    while x < width:
        points.append((x, y))
        x += rng.uniform(40, 140)
        y += rng.uniform(-jag, jag) * height
        y = min(max(y, base_y * height - jag * 2 * height), height * 0.98)
    points += [(width, y), (width, height)]
    draw.polygon(points, fill=color)
    return layer


def _bokeh(size, rng: random.Random, color, count=18, max_r=90, alpha=60) -> Image.Image:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for _ in range(count):
        r = rng.uniform(8, max_r)
        x = rng.uniform(0, size[0])
        y = rng.uniform(0, size[1])
        a = int(rng.uniform(0.3, 1.0) * alpha)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=color + (a,))
    return layer.filter(ImageFilter.GaussianBlur(10))


class LocalArtProvider(ImageProvider):
    name = "local"
    cost = CostClass.LOCAL

    def availability(self) -> tuple[bool, str]:
        return True, "built-in procedural renderer (always available)"

    def generate(self, request: ImageRequest, out_path: Path) -> Path:
        try:
            rng = random.Random(_seed_from(request))
            size = (request.width, request.height)
            style = (request.style or "CLEAN_MODERN").upper()
            if style == "CINEMATIC_MYSTICAL":
                img = self._mystical(size, rng)
            elif style == "DARK_LUXURY":
                img = self._luxury(size, rng)
            else:
                img = self._modern(size, rng)
            img = _grain(img, rng)
            img = ImageEnhance.Contrast(img).enhance(1.06)
            img = ImageEnhance.Color(img).enhance(1.08)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(out_path, "PNG")
            return out_path
        except Exception as exc:  # pragma: no cover - defensive
            raise ProviderError(f"local renderer failed: {exc}") from exc

    # ------------------------------------------------------------------
    def _mystical(self, size, rng: random.Random) -> Image.Image:
        width, height = size
        palettes = [
            [(0.0, (8, 10, 34)), (0.45, (26, 20, 64)), (0.75, (58, 34, 88)), (1.0, (16, 12, 40))],
            [(0.0, (4, 16, 32)), (0.5, (10, 44, 66)), (0.8, (36, 74, 96)), (1.0, (6, 18, 34))],
            [(0.0, (16, 8, 30)), (0.5, (54, 22, 66)), (0.8, (102, 48, 84)), (1.0, (24, 10, 36))],
        ]
        img = _vertical_gradient(size, rng.choice(palettes)).convert("RGBA")

        # stars
        stars = Image.new("RGBA", size, (0, 0, 0, 0))
        sdraw = ImageDraw.Draw(stars)
        for _ in range(rng.randint(90, 160)):
            x, y = rng.uniform(0, width), rng.uniform(0, height * 0.7)
            r = rng.uniform(0.6, 2.4)
            a = rng.randint(60, 220)
            sdraw.ellipse([x - r, y - r, x + r, y + r], fill=(235, 238, 255, a))
        img.alpha_composite(stars)

        # celestial orb (moon-like) in the upper area
        orb_x = width * rng.uniform(0.3, 0.7)
        orb_y = height * rng.uniform(0.16, 0.3)
        warm = rng.choice([(255, 216, 150), (210, 225, 255), (255, 190, 170)])
        img.alpha_composite(_radial_glow(size, (orb_x, orb_y), width * 0.75, warm, 120))
        core = ImageDraw.Draw(img)
        r0 = width * rng.uniform(0.05, 0.08)
        core.ellipse([orb_x - r0, orb_y - r0, orb_x + r0, orb_y + r0], fill=warm + (235,))
        img = Image.alpha_composite(
            img, _light_rays(size, (orb_x, orb_y), rng, warm, count=7)
        )

        # layered silhouette ridges
        img.alpha_composite(_mountains(size, rng, 0.72, (14, 12, 30, 200)))
        img.alpha_composite(_mountains(size, rng, 0.80, (8, 7, 20, 235)))
        img.alpha_composite(_mountains(size, rng, 0.88, (3, 3, 10, 255)))

        # fog bands
        fog = Image.new("RGBA", size, (0, 0, 0, 0))
        fdraw = ImageDraw.Draw(fog)
        for _ in range(4):
            y = height * rng.uniform(0.6, 0.85)
            fdraw.ellipse(
                [-width * 0.3, y, width * 1.3, y + rng.uniform(40, 120)],
                fill=(200, 205, 235, rng.randint(14, 34)),
            )
        img.alpha_composite(fog.filter(ImageFilter.GaussianBlur(30)))

        return _vignette(img.convert("RGB"), 0.6)

    def _luxury(self, size, rng: random.Random) -> Image.Image:
        width, height = size
        img = _vertical_gradient(
            size,
            [(0.0, (12, 10, 10)), (0.5, (24, 19, 14)), (0.85, (40, 30, 16)), (1.0, (14, 11, 8))],
        ).convert("RGBA")

        gold = (212, 175, 95)
        # dramatic corner light
        img.alpha_composite(
            _radial_glow(size, (width * rng.uniform(0.65, 0.9), height * rng.uniform(0.1, 0.25)),
                         width * 0.9, gold, 90)
        )
        # diagonal light shafts
        img = Image.alpha_composite(
            img,
            _light_rays(size, (width * 0.85, -height * 0.05), rng, gold, count=5, alpha=30),
        )
        # gold bokeh
        img.alpha_composite(_bokeh(size, rng, gold, count=26, max_r=70, alpha=70))
        # thin elegant accent lines
        draw = ImageDraw.Draw(img)
        for _ in range(3):
            y = height * rng.uniform(0.15, 0.9)
            x0 = width * rng.uniform(0.05, 0.25)
            x1 = width * rng.uniform(0.75, 0.95)
            draw.line([x0, y, x1, y + rng.uniform(-60, 60)], fill=gold + (26,), width=2)
        return _vignette(img.convert("RGB"), 0.5)

    def _modern(self, size, rng: random.Random) -> Image.Image:
        width, height = size
        duos = [
            [(0.0, (26, 68, 94)), (1.0, (178, 226, 226))],
            [(0.0, (36, 42, 92)), (1.0, (196, 182, 231))],
            [(0.0, (18, 82, 78)), (1.0, (214, 235, 210))],
            [(0.0, (78, 38, 78)), (1.0, (240, 204, 190))],
        ]
        img = _vertical_gradient(size, rng.choice(duos)).convert("RGBA")

        # large soft abstract shapes
        for _ in range(4):
            color = tuple(min(255, c + rng.randint(20, 70)) for c in (120, 150, 170))
            img.alpha_composite(
                _radial_glow(
                    size,
                    (width * rng.uniform(0.1, 0.9), height * rng.uniform(0.1, 0.9)),
                    width * rng.uniform(0.3, 0.6),
                    color,
                    rng.randint(40, 80),
                )
            )
        # crisp geometric rings
        rings = Image.new("RGBA", size, (0, 0, 0, 0))
        rdraw = ImageDraw.Draw(rings)
        for _ in range(3):
            r = rng.uniform(width * 0.12, width * 0.35)
            x, y = rng.uniform(0, width), rng.uniform(0, height)
            rdraw.ellipse([x - r, y - r, x + r, y + r],
                          outline=(255, 255, 255, rng.randint(24, 60)),
                          width=rng.randint(3, 10))
        img.alpha_composite(rings.filter(ImageFilter.GaussianBlur(2)))
        return _vignette(img.convert("RGB"), 0.32)
