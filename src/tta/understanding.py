"""Content understanding.

Derives, purely locally (no API calls), from the parsed metadata:

* language (de / en)
* a short TikTok hook
* cover text (2-4 highly readable words, taken from the actual content)
* visual subject, mood and style preset
* an enhanced image prompt + negative prompt
* a TikTok caption with content-derived hashtags
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .textparse import ParsedMetadata

STYLES = ("CINEMATIC_MYSTICAL", "DARK_LUXURY", "CLEAN_MODERN")

_DE_STOPWORDS = {
    "der", "die", "das", "und", "ist", "ich", "du", "sie", "wir", "ihr", "ein",
    "eine", "einen", "einem", "einer", "nicht", "mit", "für", "auf", "von",
    "dem", "den", "des", "im", "in", "an", "am", "es", "zu", "zum", "zur",
    "auch", "aber", "oder", "wenn", "dann", "was", "wie", "wer", "warum",
    "dass", "dich", "dir", "mich", "mir", "sich", "sind", "war", "hat",
    "haben", "wird", "werden", "kann", "können", "mehr", "sehr", "nur",
    "noch", "schon", "als", "aus", "bei", "nach", "über", "um", "vor",
    "diese", "dieser", "dieses", "deine", "dein", "sein", "seine", "man",
    "wirst", "bist", "hast", "so", "da", "hier", "jetzt", "immer", "alle",
    "etwas", "kein", "keine", "durch", "gegen", "ohne", "bis", "sondern",
    "manchmal", "wirklich", "vielleicht", "wieder", "ganz", "sogar",
    "deiner", "deinem", "deinen", "meiner", "meinem", "meinen", "mein",
    "meine", "seiner", "seinem", "seinen", "ihrer", "ihrem", "ihren",
    "ihre", "unser", "unsere", "unserer", "euer", "eure", "eurer",
    "dieselbe", "derselbe", "solche", "solcher", "welche", "welcher",
}

_EN_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "is", "are", "was", "were", "be",
    "been", "being", "to", "of", "in", "on", "at", "for", "with", "from",
    "by", "about", "as", "into", "than", "then", "that", "this", "these",
    "those", "it", "its", "you", "your", "yours", "i", "me", "my", "we",
    "our", "they", "their", "he", "she", "his", "her", "will", "would",
    "can", "could", "should", "shall", "may", "might", "do", "does", "did",
    "done", "have", "has", "had", "not", "no", "so", "if", "when", "what",
    "why", "how", "who", "which", "there", "here", "now", "just", "very",
    "more", "most", "some", "any", "all", "every", "out", "up", "down",
    "over", "under", "again", "only", "also", "them", "us", "get", "got",
}

_DE_MARKERS = {
    "und", "nicht", "der", "die", "das", "ist", "ein", "eine", "mit", "für",
    "wenn", "dann", "aber", "dich", "dein", "deine", "warum", "wie", "werden",
    "wird", "kann", "können", "über", "schon", "noch", "mehr", "sein", "sind",
    "haben", "dass", "auch", "nach", "bei", "aus", "zum", "zur", "immer",
}
_EN_MARKERS = {
    "the", "and", "not", "is", "a", "an", "with", "for", "if", "then", "but",
    "you", "your", "why", "how", "will", "can", "about", "already", "still",
    "more", "be", "are", "have", "that", "also", "after", "at", "from", "to",
    "always", "this", "of", "it", "on", "in",
}

# topic buckets -> style preset
_TOPIC_KEYWORDS = {
    "CINEMATIC_MYSTICAL": {
        "spirit", "spiritual", "spirituell", "seele", "soul", "universum",
        "universe", "energie", "energy", "meditation", "meditieren", "karma",
        "chakra", "astral", "engel", "angel", "mond", "moon", "sterne",
        "stars", "kosmos", "cosmos", "manifestation", "manifestieren",
        "intuition", "traum", "dream", "dreams", "träume", "mystisch",
        "mystic", "mystical", "magie", "magic", "ritual", "tarot", "aura",
        "erwachen", "awakening", "bewusstsein", "consciousness", "healing",
        "heilung", "zeichen", "sign", "signs", "schicksal", "destiny", "fate",
        "gebet", "prayer", "gott", "god", "divine", "göttlich", "believe",
        "glaube", "faith", "wunder", "miracle",
    },
    "DARK_LUXURY": {
        "geld", "money", "reich", "rich", "wealth", "reichtum", "erfolg",
        "success", "luxus", "luxury", "million", "millionär", "millionaire",
        "business", "investieren", "invest", "investment", "finanzen",
        "finance", "financial", "hustle", "unternehmer", "entrepreneur",
        "mindset", "disziplin", "discipline", "macht", "power", "status",
        "gold", "cash", "einkommen", "income", "passive", "passiv", "boss",
        "ceo", "empire", "imperium", "winner", "gewinner",
    },
    "CLEAN_MODERN": {
        "tipp", "tipps", "tip", "tips", "hack", "hacks", "app", "apps", "ki",
        "ai", "tech", "technologie", "technology", "produktiv",
        "productivity", "produktivität", "lernen", "learn", "learning",
        "study", "studium", "tutorial", "anleitung", "guide", "howto",
        "software", "tool", "tools", "routine", "gesund", "health", "fitness",
        "workout", "rezept", "recipe", "einfach", "simple", "minimal",
        "clean", "modern", "science", "wissenschaft", "fakten", "facts",
    },
}

_MOODS = {
    "CINEMATIC_MYSTICAL": "ethereal, mysterious, awe-inspiring",
    "DARK_LUXURY": "powerful, exclusive, dramatic",
    "CLEAN_MODERN": "fresh, focused, optimistic",
}

_STYLE_PROMPT = {
    "CINEMATIC_MYSTICAL": (
        "cinematic mystical atmosphere, dramatic volumetric light rays, "
        "subtle celestial imagery, glowing particles, silhouette against "
        "ethereal light, deep atmospheric perspective, fog, night sky with "
        "faint stars, rich deep blue and violet tones with warm golden accents"
    ),
    "DARK_LUXURY": (
        "dark luxury aesthetic, moody low-key lighting, elegant black and "
        "gold palette, dramatic rim light, marble and glass reflections, "
        "cinematic depth of field, premium editorial look"
    ),
    "CLEAN_MODERN": (
        "clean modern aesthetic, soft diffused studio light, minimalist "
        "composition, smooth gradients, fresh airy color palette, subtle "
        "depth of field, premium product-photography look"
    ),
}

NEGATIVE_PROMPT = (
    "text, letters, words, watermark, logo, signature, caption, subtitles, "
    "low quality, blurry, jpeg artifacts, distorted, deformed, disfigured, "
    "extra limbs, bad anatomy, oversaturated, ugly, cluttered, frame, border"
)


@dataclass
class ContentPlan:
    language: str = "en"
    hook: str = ""
    cover_text: str = ""
    visual_subject: str = ""
    visual_mood: str = ""
    style: str = "CLEAN_MODERN"
    image_prompt: str = ""
    negative_prompt: str = NEGATIVE_PROMPT
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "language": self.language,
            "hook": self.hook,
            "cover_text": self.cover_text,
            "visual_subject": self.visual_subject,
            "visual_mood": self.visual_mood,
            "style": self.style,
            "image_prompt": self.image_prompt,
            "negative_prompt": self.negative_prompt,
            "caption": self.caption,
            "hashtags": self.hashtags,
            "keywords": self.keywords,
        }


# ----------------------------------------------------------------------
def detect_language(text: str) -> str:
    """Very small de/en detector: marker words + umlauts/ß."""
    words = re.findall(r"[a-zA-ZäöüÄÖÜß]+", text.lower())
    if not words:
        return "en"
    de = sum(1 for w in words if w in _DE_MARKERS)
    en = sum(1 for w in words if w in _EN_MARKERS)
    umlauts = len(re.findall(r"[äöüÄÖÜß]", text))
    de += umlauts * 2
    return "de" if de > en else "en"


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß'\-]+", text)


def extract_keywords(text: str, language: str, limit: int = 12) -> list[str]:
    """Frequency-ranked content words (stopwords removed, title words boosted)."""
    stop = _DE_STOPWORDS if language == "de" else _EN_STOPWORDS
    counts: dict[str, float] = {}
    order: dict[str, int] = {}
    for i, tok in enumerate(_tokenize(text)):
        low = tok.lower()
        if low in stop or len(low) < 3:
            continue
        counts[low] = counts.get(low, 0.0) + 1.0
        # longer words tend to carry more meaning
        if len(low) >= 6:
            counts[low] += 0.3
        order.setdefault(low, i)
    ranked = sorted(counts, key=lambda w: (-counts[w], order[w]))
    return ranked[:limit]


def classify_style(text: str) -> str:
    lower = text.lower()
    tokens = set(re.findall(r"[a-zäöüß]+", lower))
    scores = {
        style: sum(1 for kw in kws if kw in tokens)
        for style, kws in _TOPIC_KEYWORDS.items()
    }
    best = max(scores, key=lambda s: scores[s])
    return best if scores[best] > 0 else "CLEAN_MODERN"


def _title_case(word: str) -> str:
    return word[:1].upper() + word[1:] if word else word


def make_cover_text(title: str, keywords: list[str], language: str) -> str:
    """2-4 readable words taken from the actual content."""
    stop = _DE_STOPWORDS if language == "de" else _EN_STOPWORDS
    # Prefer meaningful words from the title, in original order (dedup).
    title_words: list[str] = []
    for w in _tokenize(title):
        if w.lower() in stop:
            continue
        if w.lower() not in [p.lower() for p in title_words]:
            title_words.append(w)

    if len(title_words) > 3 and language == "de":
        # German capitalizes nouns: when we must drop words, prefer keeping
        # nouns (subject/object) over adjectives/verbs so the cover text
        # stays grammatically complete ("Disziplin schafft Wohlstand"
        # instead of the dangling "Disziplin schafft echten").
        scored = sorted(
            range(len(title_words)),
            key=lambda i: -(
                (2.0 if title_words[i][0].isupper() else 0.0)
                + len(title_words[i]) / 10.0
            ),
        )[:3]
        picked = [title_words[i] for i in sorted(scored)]
    else:
        picked = title_words[:3]
    # Top up from keywords only if the title was too thin (< 2 usable words).
    if len(picked) < 2:
        for kw in keywords:
            if len(picked) >= 3:
                break
            if kw not in [p.lower() for p in picked]:
                picked.append(kw)
    if len(picked) < 2:
        # last resort: first words of the title (even stopwords)
        fallback = _tokenize(title)[:3]
        for w in fallback:
            if w.lower() not in [p.lower() for p in picked]:
                picked.append(w)
    picked = picked[:4]
    if len(picked) > 2 and sum(len(w) for w in picked) > 26:
        picked = picked[:2]
    return " ".join(_title_case(w) for w in picked[:4]).strip()


def make_hook(title: str, description: str, language: str) -> str:
    """Short scroll-stopping hook derived from the content (<= ~8 words)."""
    source = title.strip() or description.strip()
    if not source:
        return "Watch this" if language == "en" else "Sieh dir das an"
    first_sentence = re.split(r"(?<=[.!?])\s+", source.replace("\n", " "))[0].strip()
    words = first_sentence.split()
    if len(words) <= 9:
        hook = first_sentence
    else:
        hook = " ".join(words[:8]).rstrip(",;:") + " …"
    # keep it punchy and clean: no trailing dots, no "!!!"/"???" pile-ups
    hook = hook.strip().rstrip(".")
    hook = re.sub(r"([!?])[!?]+$", r"\1", hook)
    return hook


def make_hashtags(keywords: list[str], language: str, limit: int = 5) -> list[str]:
    tags: list[str] = []
    for kw in keywords:
        slug = _ascii_slug(kw)
        if 3 <= len(slug) <= 20 and slug not in tags:
            tags.append(slug)
        if len(tags) >= limit - 1:
            break
    tags.append("fyp" if language == "en" else "fuerdich")
    return ["#" + t for t in dict.fromkeys(tags)]


def _ascii_slug(word: str) -> str:
    table = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
                           "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"})
    slug = word.translate(table)
    return re.sub(r"[^a-z0-9]", "", slug.lower())


def build_image_prompt(meta: ParsedMetadata, style: str, keywords: list[str]) -> tuple[str, str]:
    """Return (enhanced_prompt, visual_subject)."""
    if meta.image_prompt:
        subject = meta.image_prompt.strip()
    elif keywords:
        subject = "an evocative scene about " + ", ".join(keywords[:4])
    else:
        subject = "an evocative abstract scene"
    prompt = (
        f"{subject}, {_STYLE_PROMPT[style]}, vertical 9:16 composition, "
        "mobile-first framing with clear focal point in the upper two thirds, "
        "ultra detailed, 8k, professional color grading, masterpiece"
    )
    return prompt, subject


def make_caption(meta: ParsedMetadata, hashtags: list[str]) -> str:
    parts = []
    if meta.title:
        parts.append(meta.title.strip())
    if meta.description:
        desc = re.sub(r"\s+", " ", meta.description).strip()
        if len(desc) > 180:
            desc = desc[:177].rstrip() + "…"
        if desc and desc.lower() != (meta.title or "").lower():
            parts.append(desc)
    caption = " – ".join(parts) if parts else ""
    tag_str = " ".join(hashtags)
    full = (caption + "\n\n" + tag_str).strip() if tag_str else caption
    # TikTok caption limit is 2200 chars; stay well below.
    return full[:2100]


def understand(meta: ParsedMetadata) -> ContentPlan:
    combined = " ".join(
        x for x in (meta.title, meta.description, meta.image_prompt) if x
    )
    language = detect_language(f"{meta.title} {meta.description}")
    keywords = extract_keywords(f"{meta.title} {meta.title} {meta.description}", language)
    style = classify_style(combined)
    prompt, subject = build_image_prompt(meta, style, keywords)
    hashtags = make_hashtags(keywords, language)
    return ContentPlan(
        language=language,
        hook=make_hook(meta.title, meta.description, language),
        cover_text=make_cover_text(meta.title or meta.description, keywords, language),
        visual_subject=subject,
        visual_mood=_MOODS[style],
        style=style,
        image_prompt=prompt,
        negative_prompt=NEGATIVE_PROMPT,
        caption=make_caption(meta, hashtags),
        hashtags=hashtags,
        keywords=keywords,
    )
