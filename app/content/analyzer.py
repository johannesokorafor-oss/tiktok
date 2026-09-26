"""Content understanding: language, hook, caption, hashtags, mood, style.

Everything here is deterministic and offline: no external LLM call is needed,
so the pipeline has no hidden cost and produces reproducible results.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

from app.config import StylePreset
from app.parsing.text_parser import ParsedMetadata

# --------------------------------------------------------------------------
# language resources
# --------------------------------------------------------------------------
DE_STOPWORDS = set("""
der die das dass ein eine einen einem einer eines und oder aber wenn weil denn doch noch nur auch
schon ist sind war waren sein seine seinen seiner ihr ihre ihrenihrem von vom zu zum zur mit ohne
für fuer auf an am im in den dem des nicht kein keine du dich dir dein deine deinen ich mich mir
mein meine wir uns unser ihr euch es sie er man sich als wie so dann da hier dort über ueber unter
vor nach bei bis durch gegen um sehr mehr was wer wo wann warum welche welcher dieses diese dieser
hat haben hatte werden wird wurde kann können koennen muss müssen muessen soll sollte alles alle
etwas immer wieder ganz mal beim ins zum
""".split())

EN_STOPWORDS = set("""
the a an and or but if because so then than that this these those is are was were be been being am
of to in on at by for with without from into about over under after before you your yours i me my
we our us they them their he she it its as not no yes do does did done have has had will would can
could should may might must more most very just only also even still what who where when why which
how all any some each other own same too there here
""".split())

DE_MARKERS = set("""
der die das und ist nicht sich auch werden wird deine dein seele zeichen zufall erkennst spüre
spuere träume traeume wahrheit geheimnis universum bedeutung nachricht botschaft licht dunkel
""".split())

# emotional / attention weight for hook selection (both languages, lowercase)
EMOTION_WEIGHTS: Dict[str, float] = {
    # de
    "seele": 3.0, "zeichen": 3.0, "zufall": 2.6, "wahrheit": 2.8, "geheimnis": 2.8, "schicksal": 2.8,
    "erkennst": 2.4, "spürst": 2.4, "spuerst": 2.4, "botschaft": 2.4, "universum": 2.2, "intuition": 2.4,
    "traum": 2.0, "träume": 2.0, "traeume": 2.0, "licht": 1.8, "dunkelheit": 1.8, "angst": 2.0,
    "liebe": 2.0, "kraft": 1.8, "wandel": 1.8, "erwachen": 2.6, "stille": 1.8, "muster": 2.0,
    "warnung": 2.4, "verbindung": 2.0, "energie": 1.8, "wahrnehmung": 1.8, "bedeutung": 2.0,
    # en
    "soul": 3.0, "sign": 3.0, "signs": 3.0, "truth": 2.8, "secret": 2.8, "destiny": 2.8, "fate": 2.6,
    "intuition": 2.4, "message": 2.4, "universe": 2.2, "awaken": 2.6, "awakening": 2.6, "pattern": 2.0,
    "patterns": 2.0, "dream": 2.0, "dreams": 2.0, "fear": 2.0, "love": 2.0, "power": 1.8, "light": 1.8,
    "darkness": 1.8, "silence": 1.8, "warning": 2.4, "coincidence": 2.6, "meaning": 2.0, "shift": 1.8,
}

SPIRITUAL_TERMS = set("""
seele spirituell spirituelle universum energie chakra meditation karma engel schutzengel intuition
astral aura manifestation sternzeichen horoskop schicksal erwachen bewusstsein zeichen zufall
soul spiritual universe energy chakra meditation karma angel intuition aura manifestation zodiac
horoscope destiny awakening consciousness sign signs synchronicity divine sacred mystical
""".split())

LUXURY_TERMS = set("""
luxus luxury geld money reichtum wealth erfolg success business mindset disziplin discipline
premium elite invest investieren millionär millionaire karriere career
""".split())

TECH_TERMS = set("""
ki ai technologie technology software code coding app startup produkt product tutorial guide
science wissenschaft studie study fakten facts gesundheit health fitness training nutrition
""".split())

WORD_RE = re.compile(r"[0-9A-Za-zÄÖÜäöüßÀ-ÿ']+")


@dataclass
class ContentPlan:
    language: str
    hook_words: List[str]
    cover_text: str
    tiktok_title: str
    caption: str
    hashtags: List[str]
    topic: str
    subject: str
    mood: str
    visual_style: str
    style_preset: StylePreset
    image_prompt: str
    negative_prompt: str
    keywords: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["style_preset"] = self.style_preset.value
        return d


# --------------------------------------------------------------------------
def tokenize(text: str) -> List[str]:
    return [w.lower() for w in WORD_RE.findall(text or "")]


def detect_language(text: str, default: str = "de") -> str:
    """Score German vs English stopword/marker density. German is the default."""
    words = tokenize(text)
    if not words:
        return default
    de = sum(1 for w in words if w in DE_STOPWORDS) + 2 * sum(1 for w in words if w in DE_MARKERS)
    en = sum(1 for w in words if w in EN_STOPWORDS)
    # umlauts / ß are a strong German signal
    de += 3 * len(re.findall(r"[äöüÄÖÜß]", text))
    if de == en == 0:
        return default
    if de > en:
        return "de"
    if en > de:
        return "en"
    return default


def stopwords_for(lang: str) -> set:
    return DE_STOPWORDS if lang == "de" else EN_STOPWORDS


def keyword_scores(text: str, lang: str) -> Dict[str, float]:
    stop = stopwords_for(lang)
    scores: Dict[str, float] = {}
    for w in tokenize(text):
        if len(w) < 3 or w in stop or w.isdigit():
            continue
        scores[w] = scores.get(w, 0.0) + 1.0
    for w in list(scores):
        scores[w] += EMOTION_WEIGHTS.get(w, 0.0)
    return scores


def top_keywords(meta: ParsedMetadata, lang: str, limit: int = 12) -> List[str]:
    scores = keyword_scores(meta.title + " " + meta.title + " " + meta.description, lang)
    return [w for w, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


# --------------------------------------------------------------------------
# hook derivation
# --------------------------------------------------------------------------
def _norm_case(word: str) -> str:
    return word.upper()


def derive_hook(meta: ParsedMetadata, lang: str) -> List[str]:
    """Derive an ~3-word visual hook from the actual content.

    Candidate n-grams (2-4 words) are taken from the title and the strongest
    sentences of the description, then scored on content-word density,
    emotional weight, corpus frequency and length preference.  Nothing is
    invented that is not present in the source text.
    """
    if meta.cover_text:
        words = [w for w in WORD_RE.findall(meta.cover_text)][:4]
        if words:
            return [_norm_case(w) for w in words]

    stop = stopwords_for(lang)
    corpus = keyword_scores(meta.title + " " + meta.description, lang)

    sources: List[Tuple[str, float]] = [(meta.title, 1.6)]
    for sent in re.split(r"(?<=[.!?…])\s+|\n+", meta.description or ""):
        s = sent.strip()
        if 8 <= len(s) <= 180:
            sources.append((s, 1.0))
    sources = sources[:12]

    best: Optional[Tuple[float, List[str]]] = None
    for source, weight in sources:
        words = WORD_RE.findall(source)
        n = len(words)
        for size in (3, 2, 4):
            for i in range(0, max(0, n - size + 1)):
                gram = words[i:i + size]
                low = [w.lower() for w in gram]
                if low[0] in stop and low[-1] in stop:
                    continue
                content = [w for w in low if w not in stop and len(w) > 2]
                if not content:
                    continue
                if any(len(w) > 14 for w in low):
                    continue
                total_chars = sum(len(w) for w in gram) + len(gram) - 1
                if total_chars > 34:
                    continue
                score = weight
                score += sum(corpus.get(w, 0.0) for w in content)
                score += sum(EMOTION_WEIGHTS.get(w, 0.0) for w in low) * 1.5
                score += len(content) * 0.8
                score -= sum(1.4 for w in low if w in stop)
                score += 1.0 if len(content) == len(low) else 0.0
                if size == 3:
                    score += 1.2
                elif size == 2:
                    score += 0.4
                score -= max(0, total_chars - 24) * 0.15
                if best is None or score > best[0]:
                    best = (score, gram)

    if best:
        return [_norm_case(w) for w in best[1]]

    # last resort: strongest standalone keywords
    kws = [w for w, _ in sorted(corpus.items(), key=lambda kv: -kv[1])][:3]
    if kws:
        return [_norm_case(w) for w in kws]
    return [_norm_case(w) for w in WORD_RE.findall(meta.title)[:3]] or ["ANSEHEN"]


# --------------------------------------------------------------------------
# topic / style
# --------------------------------------------------------------------------
def classify_topic(meta: ParsedMetadata) -> str:
    words = set(tokenize(meta.title + " " + meta.description + " " + meta.image_prompt))
    if words & SPIRITUAL_TERMS:
        return "spiritual"
    if words & LUXURY_TERMS:
        return "luxury"
    if words & TECH_TERMS:
        return "informational"
    return "general"


def choose_style(topic: str, configured: StylePreset) -> StylePreset:
    if configured != StylePreset.AUTO:
        return configured
    return {
        "spiritual": StylePreset.CINEMATIC_MYSTICAL,
        "luxury": StylePreset.DARK_LUXURY,
        "informational": StylePreset.CLEAN_MODERN,
    }.get(topic, StylePreset.CINEMATIC_MYSTICAL)


MOODS = {
    "spiritual": "mysterious, reverent, quietly emotional, contemplative",
    "luxury": "confident, sophisticated, high-status, restrained drama",
    "informational": "clear, modern, trustworthy, energetic",
    "general": "cinematic, emotionally engaging, atmospheric",
}

STYLE_LOOK = {
    StylePreset.CINEMATIC_MYSTICAL: (
        "cinematic mystical photography, deep blues and violets with warm rim light, volumetric "
        "atmosphere, subtle celestial elements, high dynamic range, filmic contrast"
    ),
    StylePreset.DARK_LUXURY: (
        "dark luxury editorial photography, black and deep amber palette, glossy specular highlights, "
        "controlled hard light, elegant minimal composition, premium product-grade finish"
    ),
    StylePreset.CLEAN_MODERN: (
        "clean modern editorial photography, bright balanced key light, crisp detail, restrained "
        "colour palette with one strong accent, uncluttered graphic composition"
    ),
}


# --------------------------------------------------------------------------
# caption & hashtags
# --------------------------------------------------------------------------
def clean_caption_text(text: str) -> str:
    t = unicodedata.normalize("NFC", text or "")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r" +\n", "\n", t)
    return t.strip()


def _hashtagify(word: str) -> str:
    w = re.sub(r"[^0-9A-Za-zÄÖÜäöüß]", "", word)
    return w.lower()


def build_hashtags(meta: ParsedMetadata, lang: str, topic: str, limit: int = 5) -> List[str]:
    tags: List[str] = []
    seen = set()

    def add(tag: str) -> None:
        t = _hashtagify(tag)
        if len(t) >= 3 and t not in seen:
            seen.add(t)
            tags.append(t)

    for t in meta.hashtags:
        add(t)
    for kw in top_keywords(meta, lang, limit=8):
        if len(tags) >= limit:
            break
        add(kw)
    base = {
        "spiritual": ["spiritualität", "intuition"] if lang == "de" else ["spirituality", "intuition"],
        "luxury": ["mindset", "erfolg"] if lang == "de" else ["mindset", "success"],
        "informational": ["wissen"] if lang == "de" else ["learn"],
        "general": [],
    }[topic]
    for t in base:
        if len(tags) >= limit:
            break
        add(t)
    return tags[:limit]


def build_caption(meta: ParsedMetadata, lang: str, hashtags: List[str], max_chars: int) -> str:
    body = clean_caption_text(meta.description)
    # strip hashtags that already live inside the description; re-appended cleanly below
    body = re.sub(r"(?<!\w)#[0-9A-Za-zÄÖÜäöüß_]{2,40}", "", body).strip()
    body = re.sub(r"[ \t]{2,}", " ", body)
    tag_str = " ".join("#" + t for t in hashtags)
    budget = max_chars - (len(tag_str) + 2 if tag_str else 0)
    if len(body) > budget:
        cut = body[:budget]
        sep = max(cut.rfind(". "), cut.rfind("\n"), cut.rfind("! "), cut.rfind("? "))
        body = (cut[:sep + 1] if sep > budget * 0.5 else cut.rstrip()).rstrip()
    caption = (body + ("\n\n" + tag_str if tag_str else "")).strip()
    return caption[:max_chars]


def build_tiktok_title(meta: ParsedMetadata, max_chars: int = 150) -> str:
    title = clean_caption_text(meta.title).replace("\n", " ").strip()
    return title[:max_chars].rstrip()


# --------------------------------------------------------------------------
# image prompt engineering
# --------------------------------------------------------------------------
NEGATIVE_PROMPT = (
    "text, letters, words, typography, captions, subtitles, watermark, signature, logo, brand marks, "
    "UI elements, interface, frames, borders, collage, split screen, extra limbs, extra fingers, "
    "deformed hands, malformed face, disfigured, duplicate objects, cloned faces, mutated anatomy, "
    "blurry low detail background, flat lighting, cheap stock photo look, oversaturated HDR, "
    "cluttered composition, busy background, noise artifacts, jpeg artifacts, lowres, plastic skin"
)

COMPOSITION_BY_STYLE = {
    StylePreset.CINEMATIC_MYSTICAL:
        "low-angle wide cinematic shot, single silhouetted focal subject placed in the lower third, "
        "vast atmospheric sky filling the upper half, strong depth with foreground, midground and "
        "distant layers",
    StylePreset.DARK_LUXURY:
        "tight editorial medium shot, focal subject in the lower third against a deep black negative "
        "space, controlled reflections, generous clean space in the upper half",
    StylePreset.CLEAN_MODERN:
        "clean centred medium shot, subject in the lower two thirds, simple uncluttered backdrop, "
        "generous clean space in the upper third",
}


def derive_subject(meta: ParsedMetadata, lang: str, topic: str) -> str:
    if meta.image_prompt:
        first = re.split(r"[.,;]", meta.image_prompt)[0].strip()
        if len(first) > 8:
            return first
    kws = top_keywords(meta, lang, limit=5)
    default = {
        "spiritual": "a solitary human silhouette beneath a vast night sky",
        "luxury": "an elegant minimal still life under dramatic directional light",
        "informational": "a clear symbolic object representing the topic",
        "general": "a strong single focal subject representing the topic",
    }[topic]
    if kws:
        return f"{default}, evoking {', '.join(kws[:3])}"
    return default


def build_image_prompt(meta: ParsedMetadata, lang: str, topic: str,
                       style: StylePreset, hook: List[str]) -> str:
    subject = derive_subject(meta, lang, topic)
    look = STYLE_LOOK[style]
    composition = COMPOSITION_BY_STYLE[style]
    user_prompt = meta.image_prompt.strip()
    context_words = ", ".join(top_keywords(meta, lang, limit=6))

    parts = [
        f"Vertical 9:16 TikTok cover image, 1080x1920, no text anywhere in the image.",
        f"Subject: {subject}.",
        f"Topic context: {context_words}." if context_words else "",
        f"Original creative direction: {user_prompt}" if user_prompt else "",
        f"Composition: {composition}; deliberately clean, uncluttered negative space in the top 45% "
        f"of the frame reserved for typography added later.",
        f"Lighting: dramatic volumetric lighting with a single dominant light source, soft falloff, "
        f"strong separation between subject and background.",
        f"Look: {look}.",
        f"Colour: high contrast, rich shadows retaining detail, harmonious limited palette.",
        f"Emotional tone: {MOODS[topic]}.",
        f"Camera: full-frame 35mm lens equivalent, shallow-to-medium depth of field, natural "
        f"perspective, sharp focal subject, atmospheric depth behind it.",
        f"Realism: photorealistic cinematic still, film grain subtle, professional colour grading, "
        f"premium editorial quality, extremely detailed.",
        f"Framing: strictly vertical 9:16, mobile-first readability, single clear focal point.",
    ]
    return " ".join(p for p in parts if p)


# --------------------------------------------------------------------------
def analyze(meta: ParsedMetadata, *, configured_style: StylePreset = StylePreset.AUTO,
            default_language: str = "de", caption_max_chars: int = 2200,
            hook_override: Optional[str] = None,
            image_prompt_override: Optional[str] = None,
            caption_override: Optional[str] = None) -> ContentPlan:
    lang = detect_language(f"{meta.title}\n{meta.description}", default=default_language)
    topic = classify_topic(meta)
    style = choose_style(topic, StylePreset(meta.style) if meta.style in
                         {s.value for s in StylePreset} else configured_style)

    if hook_override:
        hook = [w.upper() for w in WORD_RE.findall(hook_override)][:4] or derive_hook(meta, lang)
    else:
        hook = derive_hook(meta, lang)

    hashtags = build_hashtags(meta, lang, topic)
    caption = caption_override or build_caption(meta, lang, hashtags, caption_max_chars)
    image_prompt = image_prompt_override or build_image_prompt(meta, lang, topic, style, hook)

    return ContentPlan(
        language=lang,
        hook_words=hook,
        cover_text=" ".join(hook),
        tiktok_title=build_tiktok_title(meta),
        caption=caption,
        hashtags=hashtags,
        topic=topic,
        subject=derive_subject(meta, lang, topic),
        mood=MOODS[topic],
        visual_style=STYLE_LOOK[style],
        style_preset=style,
        image_prompt=image_prompt,
        negative_prompt=NEGATIVE_PROMPT,
        keywords=top_keywords(meta, lang),
    )
