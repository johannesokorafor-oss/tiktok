"""Content understanding: language, hook, caption, hashtags, mood, style.

Everything here is deterministic and offline: no external LLM call is needed,
so the pipeline has no hidden cost and produces reproducible results.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

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
    """Everything the application derives from the .txt sidecar.

    Thumbnails/covers are intentionally NOT part of this: the creator makes and
    selects the cover manually inside TikTok. ``image_prompt`` is only carried
    through from the sidecar for the creator's own later use and never triggers
    any image generation.
    """

    language: str
    tiktok_title: str
    caption: str
    hashtags: List[str]
    topic: str
    keywords: List[str] = field(default_factory=list)
    #: verbatim IMAGE_PROMPT from the .txt (optional, purely informational)
    image_prompt: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


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







# --------------------------------------------------------------------------
def analyze(meta: ParsedMetadata, *, default_language: str = "de",
            caption_max_chars: int = 2200,
            caption_override: Optional[str] = None,
            title_override: Optional[str] = None) -> ContentPlan:
    """Derive language, title, caption and hashtags from the parsed sidecar.

    No image/cover work happens here - the creator handles the thumbnail in
    TikTok.
    """
    lang = detect_language(meta.title + " " + meta.description, default=default_language)
    topic = classify_topic(meta)
    hashtags = build_hashtags(meta, lang, topic)
    caption = caption_override or build_caption(meta, lang, hashtags, caption_max_chars)

    return ContentPlan(
        language=lang,
        tiktok_title=title_override or build_tiktok_title(meta),
        caption=caption,
        hashtags=hashtags,
        topic=topic,
        keywords=top_keywords(meta, lang),
        image_prompt=meta.image_prompt.strip(),
    )
