"""Offline procedural background generator (LOCAL, no network, no cost).

This is *not* a mock: it really renders an original 9:16 cinematic
background with Pillow - layered colour fields derived deterministically
from the prompt text, atmospheric light shafts, depth haze, particles,
a horizon/silhouette band, vignette and grain.  It is used as the last
fallback so the pipeline can always produce a valid, usable cover when no
AI image backend is reachable (offline machine, provider outage, CI).

Quality is obviously below a diffusion model; the dashboard and logs mark
covers produced this way so the user can regenerate later.
"""
from __future__ import annotations

import hashlib
import io
import math
import random

from PIL import Image, ImageDraw, ImageFilter

from app.config import ProviderCost
from app.images.base import ImageProvider, ImageRequest, ImageResult

PALETTES = {
    "mystical": [(8, 10, 32), (26, 22, 74), (68, 44, 120), (176, 140, 220), (250, 226, 180)],
    "luxury": [(6, 5, 4), (28, 20, 10), (78, 54, 18), (176, 132, 48), (246, 226, 168)],
    "modern": [(12, 18, 28), (24, 46, 74), (46, 92, 140), (126, 178, 214), (240, 246, 250)],
    "nature": [(6, 16, 12), (14, 42, 32), (34, 78, 56), (108, 156, 110), (226, 238, 206)],
}


def _pick_palette(prompt: str) -> list:
    p = prompt.lower()
    if any(w in p for w in ("luxury", "gold", "money", "wealth", "black", "elegant")):
        return PALETTES["luxury"]
    if any(w in p for w in ("clean", "modern", "bright", "studio", "tech", "science")):
        return PALETTES["modern"]
    if any(w in p for w in ("forest", "nature", "green", "mountain", "ocean", "wald")):
        return PALETTES["nature"]
    return PALETTES["mystical"]


def _lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def _vertical_gradient(size, stops):
    w, h = size
    img = Image.new("RGB", (1, h))
    px = img.load()
    n = len(stops) - 1
    for y in range(h):
        t = y / max(1, h - 1)
        seg = min(int(t * n), n - 1)
        local = (t * n) - seg
        px[0, y] = _lerp(stops[seg], stops[seg + 1], local)
    return img.resize((w, h), Image.BILINEAR)


class OfflineProvider(ImageProvider):
    name = "offline"
    declared_cost = ProviderCost.LOCAL
    declared_source = "documented"
    cost_note = ("Pure local Pillow rendering. No network, no cost. This is a *technical "
                 "fallback*, NOT an AI image model - covers made this way are labelled "
                 "OFFLINE_FALLBACK_GENERATED everywhere.")
    requires_network = False
    #: the decisive flag: this renderer is not a generative image model
    is_generative = False

    @property
    def model_name(self) -> str:
        return "pillow-procedural"

    def is_configured(self) -> bool:
        return self.settings.allow_offline_image_fallback

    def generate(self, request: ImageRequest) -> ImageResult:
        seed = request.seed if request.seed is not None else int(
            hashlib.sha256(request.prompt.encode("utf-8")).hexdigest()[:8], 16)
        rnd = random.Random(seed)
        w, h = request.width, request.height
        palette = _pick_palette(request.prompt)

        # base sky gradient (dark top -> luminous horizon -> dark foreground)
        base = _vertical_gradient((w, h), [palette[0], palette[1], palette[2], palette[1], palette[0]])
        img = base.convert("RGB")
        draw = ImageDraw.Draw(img, "RGBA")

        # --- distant glow / key light source
        gx = int(w * rnd.uniform(0.3, 0.7))
        gy = int(h * rnd.uniform(0.42, 0.58))
        glow = Image.new("RGB", (w, h), (0, 0, 0))
        gd = ImageDraw.Draw(glow)
        for r in range(int(min(w, h) * 0.55), 0, -12):
            t = 1 - r / (min(w, h) * 0.55)
            col = _lerp((0, 0, 0), palette[4], t ** 2.2)
            gd.ellipse([gx - r, gy - int(r * 0.85), gx + r, gy + int(r * 0.85)], fill=col)
        glow = glow.filter(ImageFilter.GaussianBlur(min(w, h) * 0.05))
        img = Image.blend(img, glow, 0.35)

        draw = ImageDraw.Draw(img, "RGBA")

        # --- particles / stars in the upper (text-safe) region, kept subtle
        for _ in range(int(320 * (1.0 if request.quality != "FAST" else 0.5))):
            x = rnd.randrange(0, w)
            y = int(abs(rnd.gauss(0, 0.38)) * h * 0.6)
            if y >= h:
                continue
            rad = rnd.choice([1, 1, 1, 2, 2, 3])
            alpha = rnd.randint(40, 190)
            draw.ellipse([x - rad, y - rad, x + rad, y + rad], fill=(255, 250, 235, alpha))

        # --- volumetric light shafts from the key light
        shafts = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shafts)
        for _ in range(rnd.randint(4, 7)):
            ang = math.radians(rnd.uniform(-38, 38))
            spread = rnd.uniform(0.012, 0.05)
            length = h * 1.2
            x1 = gx + math.sin(ang - spread) * length
            x2 = gx + math.sin(ang + spread) * length
            y2 = gy - math.cos(ang) * length
            sd.polygon([(gx, gy), (x1, y2), (x2, y2)],
                       fill=(*palette[4], rnd.randint(16, 46)))
        shafts = shafts.filter(ImageFilter.GaussianBlur(w * 0.03))
        img = Image.alpha_composite(img.convert("RGBA"), shafts).convert("RGB")
        draw = ImageDraw.Draw(img, "RGBA")

        # --- atmospheric haze bands for depth
        haze = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        hd = ImageDraw.Draw(haze)
        for i in range(6):
            y = int(h * (0.45 + i * 0.045))
            hd.rectangle([0, y, w, y + int(h * 0.03)], fill=(*palette[3], 26 - i * 3))
        haze = haze.filter(ImageFilter.GaussianBlur(h * 0.012))
        img = Image.alpha_composite(img.convert("RGBA"), haze).convert("RGB")
        draw = ImageDraw.Draw(img, "RGBA")

        # --- layered horizon silhouettes (foreground depth, keeps top clean)
        for layer, (base_y, darkness) in enumerate([(0.70, 120), (0.78, 180), (0.88, 235)]):
            pts = [(0, h)]
            y0 = h * base_y
            amp = h * (0.035 - layer * 0.008)
            phase = rnd.uniform(0, math.tau)
            for x in range(0, w + 1, 12):
                yy = y0 + math.sin(x / w * math.tau * rnd.uniform(0.8, 1.4) + phase) * amp \
                     + math.sin(x / w * math.tau * 3 + phase) * amp * 0.35
                pts.append((x, yy))
            pts.append((w, h))
            draw.polygon(pts, fill=(int(palette[0][0] * 0.6), int(palette[0][1] * 0.6),
                                    int(palette[0][2] * 0.6), darkness))

        # --- central silhouette subject (grounded focal point, lower third)
        sxc = int(w * rnd.uniform(0.38, 0.62))
        ground = int(h * 0.86)
        fig_h = int(h * 0.17)
        fw = max(6, int(fig_h * 0.13))
        head_r = int(fig_h * 0.085)
        body = [(sxc - fw, ground), (sxc - int(fw * 0.7), ground - int(fig_h * 0.72)),
                (sxc + int(fw * 0.7), ground - int(fig_h * 0.72)), (sxc + fw, ground)]
        draw.polygon(body, fill=(4, 4, 8, 255))
        draw.ellipse([sxc - head_r, ground - int(fig_h * 0.72) - head_r * 2,
                      sxc + head_r, ground - int(fig_h * 0.72)], fill=(4, 4, 8, 255))

        # --- vignette
        vign = Image.new("L", (w, h), 0)
        vd = ImageDraw.Draw(vign)
        vd.ellipse([-int(w * 0.35), -int(h * 0.18), int(w * 1.35), int(h * 1.18)], fill=255)
        vign = vign.filter(ImageFilter.GaussianBlur(min(w, h) * 0.12))
        img = Image.composite(img, Image.new("RGB", (w, h), (0, 0, 0)), vign)

        # --- fine grain
        grain = Image.effect_noise((w, h), 14).convert("L").point(lambda v: int(v * 0.35 + 82))
        img = Image.blend(img, Image.merge("RGB", (grain, grain, grain)), 0.05)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return ImageResult(data=buf.getvalue(), mime="image/png", provider=self.name, seed=seed,
                           generative=False,
                           meta={"renderer": "pillow-procedural", "ai_model": False})

    def health(self) -> tuple[bool, str]:
        if not self.settings.allow_offline_image_fallback:
            return False, "disabled (ALLOW_OFFLINE_IMAGE_FALLBACK=false)"
        return True, "available (local procedural fallback renderer, not an AI model)"
