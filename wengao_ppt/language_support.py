"""Lightweight, offline language hints and support matrix for multilingual presentation inputs.

The detector is intentionally advisory: Unicode script and small lexical cues
help the local planner choose a language-aware reading path, but the planner
must inspect the original text and may not reject a deck solely because a hint
is mixed or unknown. No source text leaves the machine for language detection.

The language support matrix uses publicly cited demographic data (total speaker
population > 20 million, cited from Ethnologue 2024, 27th edition / UNESCO /
Wikipedia World Languages) and classifies support across 5 explicit functional
layers:
  1. text_recognition (Unicode script / lexical detection)
  2. semantic_planning (local model understanding & continuous excerpt extraction)
  3. visual_query_generation (neutral English visual query synthesis)
  4. typography_and_layout (RTL/CJK/complex script handling, font fallback)
  5. generation_and_gate (actual image retrieval, aspect-ratio safe placement, gate verification)

The current release acceptance target is pinned to the 26 existing matrix
entries whose recorded speaker estimate is above 20 million. Greek and Hebrew
remain extra script-detection hints outside this release target. This is a
version scope decision, not a claim that the target is an exhaustive worldwide
list. Only languages verified by end-to-end tests receive FULL_SUPPORT; others
remain PARTIAL_SUPPORT or PENDING_VERIFICATION.
"""
from __future__ import annotations

import re
import unicodedata
import hashlib
import json

POPULATION_DATA_SOURCE = "Ethnologue (2024, 27th edition) / UNESCO / Wikipedia World Languages (Total Speakers > 20M)"
LANGUAGE_DETECTOR_VERSION = "unicode-script-lexical-v4"

SUPPORTED_MAJOR_LANGUAGES = {
    "ar": "Arabic", "bn": "Bengali", "de": "German", "el": "Greek", "en": "English",
    "es": "Spanish", "fa": "Persian", "fr": "French", "hi": "Hindi",
    "he": "Hebrew", "id": "Indonesian", "it": "Italian", "ja": "Japanese", "ko": "Korean",
    "ms": "Malay", "nl": "Dutch", "pl": "Polish", "pt": "Portuguese",
    "ru": "Russian", "sw": "Swahili", "ta": "Tamil", "te": "Telugu",
    "th": "Thai", "tr": "Turkish",
    "uk": "Ukrainian", "ur": "Urdu", "vi": "Vietnamese", "zh": "Chinese",
}

# Explicit Support Matrix: Languages with total speaker population > 20 million
# (plus Greek & Hebrew retained as classic historical/RTL script detection hints).
LANGUAGE_SUPPORT_MATRIX = {
    "en": {
        "name": "English", "native_name": "English", "script": "Latin", "direction": "ltr",
        "speakers_millions": 1500, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "VERIFIED",
            "visual_query_generation": "VERIFIED", "typography_and_layout": "VERIFIED",
            "generation_and_gate": "VERIFIED",
        },
        "support_status": "FULL_SUPPORT",
        "verified_evidence": ["UE36", "synthetic_multilingual_gates", "benchmark_13p"],
    },
    "zh": {
        "name": "Chinese", "native_name": "中文", "script": "Han", "direction": "ltr",
        "speakers_millions": 1140, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "VERIFIED",
            "visual_query_generation": "VERIFIED", "typography_and_layout": "VERIFIED",
            "generation_and_gate": "VERIFIED",
        },
        "support_status": "FULL_SUPPORT",
        "verified_evidence": ["UE01-UE35", "50_scene_fixture", "benchmark_13p", "job_58c8be3dda38"],
    },
    "hi": {
        "name": "Hindi", "native_name": "हिन्दी", "script": "Devanagari", "direction": "ltr",
        "speakers_millions": 609, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "VERIFIED",
            "visual_query_generation": "VERIFIED", "typography_and_layout": "PARTIAL",
            "generation_and_gate": "PARTIAL",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["cross_script_hindi_verification", "devanagari_scene_mapping"],
    },
    "es": {
        "name": "Spanish", "native_name": "Español", "script": "Latin", "direction": "ltr",
        "speakers_millions": 559, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "VERIFIED",
            "visual_query_generation": "VERIFIED", "typography_and_layout": "PARTIAL",
            "generation_and_gate": "PARTIAL",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["cross_script_spanish_lexical", "spanish_consumer_dedupe"],
    },
    "ar": {
        "name": "Arabic", "native_name": "العربية", "script": "Arabic", "direction": "rtl",
        "speakers_millions": 332, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "VERIFIED",
            "visual_query_generation": "VERIFIED", "typography_and_layout": "PARTIAL",
            "generation_and_gate": "PARTIAL",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["cross_script_arabic_rtl_verification", "rtl_neutral_query"],
    },
    "fr": {
        "name": "French", "native_name": "Français", "script": "Latin", "direction": "ltr",
        "speakers_millions": 310, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "PARTIAL",
            "visual_query_generation": "PARTIAL", "typography_and_layout": "PENDING",
            "generation_and_gate": "PENDING",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["latin_lexical_french"],
    },
    "bn": {
        "name": "Bengali", "native_name": "বাংলা", "script": "Bengali", "direction": "ltr",
        "speakers_millions": 278, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "VERIFIED",
            "visual_query_generation": "VERIFIED", "typography_and_layout": "PARTIAL",
            "generation_and_gate": "PARTIAL",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["bengali_script_verification", "bengali_scene_mapping"],
    },
    "pt": {
        "name": "Portuguese", "native_name": "Português", "script": "Latin", "direction": "ltr",
        "speakers_millions": 264, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "PARTIAL",
            "visual_query_generation": "PARTIAL", "typography_and_layout": "PENDING",
            "generation_and_gate": "PENDING",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["latin_lexical_portuguese"],
    },
    "ru": {
        "name": "Russian", "native_name": "Русский", "script": "Cyrillic", "direction": "ltr",
        "speakers_millions": 255, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "PARTIAL",
            "visual_query_generation": "PARTIAL", "typography_and_layout": "PENDING",
            "generation_and_gate": "PENDING",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["cyrillic_script_detection"],
    },
    "ur": {
        "name": "Urdu", "native_name": "اردو", "script": "Arabic", "direction": "rtl",
        "speakers_millions": 238, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "PARTIAL",
            "visual_query_generation": "PARTIAL", "typography_and_layout": "PENDING",
            "generation_and_gate": "PENDING",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["urdu_script_markers"],
    },
    "id": {
        "name": "Indonesian", "native_name": "Bahasa Indonesia", "script": "Latin", "direction": "ltr",
        "speakers_millions": 200, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "VERIFIED",
            "visual_query_generation": "VERIFIED", "typography_and_layout": "PARTIAL",
            "generation_and_gate": "PARTIAL",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["indonesian_lexical_verification", "indonesian_scene_mapping"],
    },
    "de": {
        "name": "German", "native_name": "Deutsch", "script": "Latin", "direction": "ltr",
        "speakers_millions": 133, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "PARTIAL",
            "visual_query_generation": "PARTIAL", "typography_and_layout": "PENDING",
            "generation_and_gate": "PENDING",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["latin_lexical_german"],
    },
    "ja": {
        "name": "Japanese", "native_name": "日本語", "script": "Han/Kana", "direction": "ltr",
        "speakers_millions": 125, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "PARTIAL",
            "visual_query_generation": "PARTIAL", "typography_and_layout": "PENDING",
            "generation_and_gate": "PENDING",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["hiragana_katakana_detection"],
    },
    "te": {
        "name": "Telugu", "native_name": "తెలుగు", "script": "Telugu", "direction": "ltr",
        "speakers_millions": 96, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "PENDING",
            "visual_query_generation": "PENDING", "typography_and_layout": "PENDING",
            "generation_and_gate": "PENDING",
        },
        "support_status": "PENDING_VERIFICATION",
        "verified_evidence": ["telugu_script_range"],
    },
    "tr": {
        "name": "Turkish", "native_name": "Türkçe", "script": "Latin", "direction": "ltr",
        "speakers_millions": 88, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "PARTIAL",
            "visual_query_generation": "PARTIAL", "typography_and_layout": "PENDING",
            "generation_and_gate": "PENDING",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["latin_lexical_turkish"],
    },
    "ta": {
        "name": "Tamil", "native_name": "தமிழ்", "script": "Tamil", "direction": "ltr",
        "speakers_millions": 86, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "PENDING",
            "visual_query_generation": "PENDING", "typography_and_layout": "PENDING",
            "generation_and_gate": "PENDING",
        },
        "support_status": "PENDING_VERIFICATION",
        "verified_evidence": ["tamil_script_range"],
    },
    "vi": {
        "name": "Vietnamese", "native_name": "Tiếng Việt", "script": "Latin", "direction": "ltr",
        "speakers_millions": 85, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "PARTIAL",
            "visual_query_generation": "PARTIAL", "typography_and_layout": "PENDING",
            "generation_and_gate": "PENDING",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["latin_lexical_vietnamese"],
    },
    "ko": {
        "name": "Korean", "native_name": "한국어", "script": "Hangul", "direction": "ltr",
        "speakers_millions": 82, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "PARTIAL",
            "visual_query_generation": "PARTIAL", "typography_and_layout": "PENDING",
            "generation_and_gate": "PENDING",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["hangul_script_detection"],
    },
    "fa": {
        "name": "Persian", "native_name": "فارسی", "script": "Arabic", "direction": "rtl",
        "speakers_millions": 80, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "PARTIAL",
            "visual_query_generation": "PARTIAL", "typography_and_layout": "PENDING",
            "generation_and_gate": "PENDING",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["persian_script_markers"],
    },
    "sw": {
        "name": "Swahili", "native_name": "Kiswahili", "script": "Latin", "direction": "ltr",
        "speakers_millions": 70, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "PARTIAL",
            "visual_query_generation": "PARTIAL", "typography_and_layout": "PENDING",
            "generation_and_gate": "PENDING",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["swahili_lexical_detection"],
    },
    "it": {
        "name": "Italian", "native_name": "Italiano", "script": "Latin", "direction": "ltr",
        "speakers_millions": 68, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "PARTIAL",
            "visual_query_generation": "PARTIAL", "typography_and_layout": "PENDING",
            "generation_and_gate": "PENDING",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["latin_lexical_italian"],
    },
    "th": {
        "name": "Thai", "native_name": "ไทย", "script": "Thai", "direction": "ltr",
        "speakers_millions": 61, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "PARTIAL",
            "visual_query_generation": "PARTIAL", "typography_and_layout": "PENDING",
            "generation_and_gate": "PENDING",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["thai_script_detection"],
    },
    "pl": {
        "name": "Polish", "native_name": "Polski", "script": "Latin", "direction": "ltr",
        "speakers_millions": 41, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "PARTIAL",
            "visual_query_generation": "PARTIAL", "typography_and_layout": "PENDING",
            "generation_and_gate": "PENDING",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["latin_lexical_polish"],
    },
    "uk": {
        "name": "Ukrainian", "native_name": "Українська", "script": "Cyrillic", "direction": "ltr",
        "speakers_millions": 40, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "PARTIAL",
            "visual_query_generation": "PARTIAL", "typography_and_layout": "PENDING",
            "generation_and_gate": "PENDING",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["ukrainian_cyrillic_markers"],
    },
    "ms": {
        "name": "Malay", "native_name": "Bahasa Melayu", "script": "Latin", "direction": "ltr",
        "speakers_millions": 33, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "PARTIAL",
            "visual_query_generation": "PARTIAL", "typography_and_layout": "PENDING",
            "generation_and_gate": "PENDING",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["latin_lexical_malay"],
    },
    "nl": {
        "name": "Dutch", "native_name": "Nederlands", "script": "Latin", "direction": "ltr",
        "speakers_millions": 30, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "PARTIAL",
            "visual_query_generation": "PARTIAL", "typography_and_layout": "PENDING",
            "generation_and_gate": "PENDING",
        },
        "support_status": "PARTIAL_SUPPORT",
        "verified_evidence": ["latin_lexical_dutch"],
    },
    # Classical script detection hints retained for detection fallback
    "el": {
        "name": "Greek", "native_name": "Ελληνικά", "script": "Greek", "direction": "ltr",
        "speakers_millions": 13, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "PENDING",
            "visual_query_generation": "PENDING", "typography_and_layout": "PENDING",
            "generation_and_gate": "PENDING",
        },
        "support_status": "PENDING_VERIFICATION",
        "verified_evidence": ["greek_script_range"],
    },
    "he": {
        "name": "Hebrew", "native_name": "עברית", "script": "Hebrew", "direction": "rtl",
        "speakers_millions": 9, "source_year": "2024",
        "layers": {
            "text_recognition": "VERIFIED", "semantic_planning": "PENDING",
            "visual_query_generation": "PENDING", "typography_and_layout": "PENDING",
            "generation_and_gate": "PENDING",
        },
        "support_status": "PENDING_VERIFICATION",
        "verified_evidence": ["hebrew_script_range"],
    },
}

# V1 acceptance scope: freeze the candidate's existing >20M language entries.
# Later language additions are intentionally deferred to a subsequent release.
CURRENT_LANGUAGE_ACCEPTANCE_SCOPE_ID = "existing-matrix-26-v1"
CURRENT_LANGUAGE_ACCEPTANCE_CODES = (
    "ar", "bn", "de", "en", "es", "fa", "fr", "hi", "id", "it", "ja",
    "ko", "ms", "nl", "pl", "pt", "ru", "sw", "ta", "te", "th", "tr",
    "uk", "ur", "vi", "zh",
)
OUT_OF_SCOPE_LANGUAGE_HINT_CODES = ("el", "he")

#: Latin-script lexical markers for the 13 in-scope Latin languages.
#: Function words alone are not sufficient evidence: Portuguese and Spanish
#: share "a/e/o/de", and a bare URL such as "https://example.com/a/b" used to
#: score Portuguese from the domain label plus one article. Each entry
#: therefore carries everyday content words (business/consumer/health
#: vocabulary) so a match reflects actual language, not punctuation or a
#: brand name. Markers are diacritic-free because _plain_latin strips accents.
_LATIN_MARKERS = {
    "de": set("der die das und mit fuer von zu den eine einer ist sind im am des auf "
              "verbraucher verbrauchern kaufen kauft lebensmittel supermarkt preise "
              "vergleichen vergleicht bericht unternehmen mitarbeiter kunden wachstum "
              "umsatz markt".split()),
    "en": set("the and of to in for with from is are this that user users customer "
              "customers consumer consumers product products market markets store "
              "stores retail browsing shopping shop shops shopper shoppers "
              "supermarket supermarkets grocery groceries aisle aisles browse "
              "produce checkout cashier receipt sales business company system data "
              "team workers worker staff patient patients student students growth "
              "revenue report camera battery life price prices".split()),
    "es": set("el la los las y de que para con del una un por es son en "
              "consumidores usuarios supermercado supermercados compra compran "
              "alimentos pasillos precios comparan informe empresa clientes mercado "
              "crecimiento ventas".split()),
    "fr": set("le la les et des pour avec dans une un est sont du de aux utilisateurs "
              "consommateurs achetent produits supermarche supermarches comparent "
              "prix rapport entreprise clients marche croissance ventes magasin".split()),
    "id": set("dan yang untuk dengan dari pada ini itu adalah pengguna konsumen "
              "pelanggan pasien klinik berbicara dokter perawatan membeli harga toko "
              "pasar barang penjualan pembelian laporan perusahaan".split()),
    "it": set("il lo la gli e di per con una un che nel della utenti consumatori "
              "acquistano cibo supermercato supermercati prezzi confrontano rapporto "
              "azienda clienti mercato crescita vendite negozio".split()),
    # Malay and Indonesian share many high-frequency markers. Add
    # Malaysia-specific content words so ordinary Malay business materials
    # are less likely to fall through as ambiguous; detection stays advisory.
    "ms": set("dan yang untuk dengan dari pada ini itu adalah pengguna barangan "
              "perkhidmatan pasar raya pelanggan membeli harga kedai jualan "
              "pembelian laporan syarikat perniagaan".split()),
    "nl": set("de het een en van voor met op is zijn gebruikers consumenten kopen "
              "levensmiddelen supermarkt prijzen vergelijken rapport bedrijf klanten "
              "markt groei omzet winkel".split()),
    "pt": set("o a os as e de para com uma um que por no na consumidores usuarios "
              "supermercado supermercados compram alimentos precos comparam "
              "relatorio empresa clientes mercado crescimento vendas loja".split()),
    "pl": set("pacjent pacjenci klinika klinice kliniki rozmawia lekarz lekarzem "
              "leczeniu leczenie szpital przychodnia cena ceny zakupy sklep rynek "
              "sprzedaz firma klienci wzrost raport".split()),
    "sw": set("na ya kwa ni wa kutoka katika hii kuwa watu mgonjwa wagonjwa kliniki "
              "kuzungumza daktari wateja mteja wanunua kununua bidhaa duka masoko "
              "sokoni bei biashara hospitali kampuni mauzo ripoti".split()),
    "tr": set("ve bir icin ile bu olarak kullanici kullanicilar tuketici tuketiciler "
              "hasta klinik klinikte doktor doktorla konusur ziyaret magaza market "
              "supermarket urun fiyat fiyatlari satin alisveris musteri sirket satis "
              "rapor".split()),
    "vi": set("va cua cho trong voi nguoi la nhung duoc khach hang mua ban gia sieu "
              "thi benh nhan bac si phong kham cong ty bao cao thi truong tang "
              "trong doanh so cua hang".split()),
}

#: URL/email noise is stripped before script counting and lexical scoring.
#: A bare link is not evidence of Portuguese ("com") or Spanish ("a").
_NOISE_RE = re.compile(
    r"(?:https?://\S+|www\.\S+|[^\s@]+@[^\s@]+\.[A-Za-z]{2,})")

#: A page must carry at least this many Latin letters before lexical scoring
#: is attempted; below it the hint is reported as uncertain, never guessed.
_LATIN_MIN_LETTERS = 8
#: A second script needs at least this many characters to count as present.
_NON_LATIN_MIN = 2

#: Characters per semantic unit, used for cross-script dominance. Logographic
#: and syllabic scripts count one unit per character; alphabetic scripts need
#: roughly 3.5 characters to carry the same amount of content.
_CHARS_PER_UNIT = {
    "Han": 1.0, "Hiragana": 1.0, "Katakana": 1.0, "Hangul": 1.0,
    "Thai": 1.0, "Tamil": 1.0, "Telugu": 1.0,
    "Devanagari": 2.5, "Bengali": 2.5, "Arabic": 3.0, "Hebrew": 3.0,
    "Cyrillic": 3.0, "Greek": 3.0, "Latin": 3.5,
}


def _plain_latin(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _script_counts(text: str) -> dict[str, int]:
    ranges = {
        "Han": ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF)),
        "Hiragana": ((0x3040, 0x309F),),
        "Katakana": ((0x30A0, 0x30FF), (0x31F0, 0x31FF)),
        "Hangul": ((0xAC00, 0xD7AF), (0x1100, 0x11FF)),
        "Cyrillic": ((0x0400, 0x052F),),
        "Arabic": ((0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF)),
        "Devanagari": ((0x0900, 0x097F),),
        "Bengali": ((0x0980, 0x09FF),),
        "Thai": ((0x0E00, 0x0E7F),),
        "Tamil": ((0x0B80, 0x0BFF),),
        "Telugu": ((0x0C00, 0x0C7F),),
        "Greek": ((0x0370, 0x03FF),),
        "Hebrew": ((0x0590, 0x05FF),),
    }
    result = {}
    for script, intervals in ranges.items():
        result[script] = sum(
            1 for char in text
            if (unicodedata.category(char)[0] in {"L", "M"}
                and any(start <= ord(char) <= end for start, end in intervals))
        )
    result["Latin"] = sum(
        1 for char in text if char.isalpha() and "LATIN" in unicodedata.name(char, "")
    )
    return {key: value for key, value in result.items() if value}


def _latin_tokens(text: str) -> list[str]:
    """Latin word tokens with URLs, e-mails and host labels removed."""
    cleaned = _NOISE_RE.sub(" ", str(text or ""))
    return re.findall(r"[a-z]+", _plain_latin(cleaned))


def _marker_matches(tokens: set[str], markers: set[str]) -> set[str]:
    """Exact matches plus bounded inflection matches for content stems.

    Agglutinative and inflected languages (Swahili "anazungumza", Turkish
    "tüketiciler", Polish "lekarzem", English "shoppers") do not appear
    verbatim in a marker list. A marker of six or more letters may therefore
    match anywhere inside a token, and a marker of four or more letters may
    match as a bounded prefix (at most four extra characters). Two-letter
    function words never match loosely. Only the token is recorded, so scores
    stay distinct-token counts.
    """
    matched = set()
    for token in tokens:
        if token in markers:
            matched.add(token)
            continue
        hit = False
        if len(token) >= 6:
            # Infix inflection: Swahili "anazungumza" carries "zungumza".
            hit = any(len(marker) >= 6 and marker in token for marker in markers)
        if not hit and len(token) >= 4:
            # Bounded prefix inflection: English "shoppers"/"shopping" from
            # "shop", Turkish "tuketiciler" from "tuketici". The suffix window
            # keeps this from matching unrelated long words.
            hit = any(len(marker) >= 4 and token.startswith(marker)
                      and len(token) - len(marker) <= 4 for marker in markers)
        if hit:
            matched.add(token)
    return matched


def _latin_language_evidence(text: str) -> tuple[str, str, list[str]]:
    """Return (code, confidence, matched tokens) for Latin-script text."""
    tokens = set(_latin_tokens(text))
    if not tokens:
        return "und", "unknown", []
    hits = {}
    for code, markers in _LATIN_MARKERS.items():
        matched = _marker_matches(tokens, markers)
        if matched:
            hits[code] = matched
    if not hits:
        return "und", "ambiguous", []
    ranked = sorted(hits.items(), key=lambda item: (-len(item[1]), item[0]))
    best_code, best_matches = ranked[0]
    second_score = len(ranked[1][1]) if len(ranked) > 1 else 0
    # Two short function words alone are not language evidence (a bare link
    # used to score Portuguese from ".com" plus "a"). Accept either at least
    # three distinct matches or one matched content word of four or more
    # letters; function-word-only sentences such as Spanish
    # "Registro del paciente en la clínica" still qualify through the count.
    has_content_evidence = any(len(token) >= 4 for token in best_matches)
    if (len(best_matches) < 2 or len(best_matches) <= second_score
            or not (len(best_matches) >= 3 or has_content_evidence)):
        return "und", "ambiguous", sorted(best_matches)
    return best_code, "lexical", sorted(best_matches)


def _latin_language(text: str) -> tuple[str, str]:
    code, confidence, _ = _latin_language_evidence(text)
    return code, confidence


def detect_language_hint(text: str) -> dict:
    """Return a stable, privacy-safe language hint with explicit evidence.

    The hint is advisory and never rejects a page. It reports four things the
    planner and the acceptance tests can audit separately:

    * ``primary`` - the dominant language of the page, or ``mul`` when two
      non-Latin scripts are both substantive, or ``und`` when nothing can be
      established (numbers, emoji, bare links, very short labels).
    * ``languages`` - every language for which there is script or lexical
      evidence. For a Chinese page with English prose this is ``["zh","en"]``.
    * ``mixed`` - True when a second language is present with real prose
      evidence rather than a brand name or a unit.
    * ``script_shares`` / ``script_unit_shares`` - per-script letter share and
      cross-script semantic-unit share, so a reviewer can see why a page was
      treated as dominant Chinese instead of mixed.
    """
    raw = str(text or "")
    counts = _script_counts(_NOISE_RE.sub(" ", raw))
    total_letters = sum(counts.values())
    if not total_letters:
        return {"primary": "und", "languages": [], "scripts": [],
                "confidence": "unknown", "detector": LANGUAGE_DETECTOR_VERSION,
                "mixed": False, "mixed_languages": [], "script_shares": {},
                "lexical_evidence": [], "uncertain": True}

    non_latin = {script: count for script, count in counts.items()
                 if script != "Latin" and count >= _NON_LATIN_MIN}
    languages = []
    is_japanese = "Hiragana" in non_latin or "Katakana" in non_latin
    has_hangul = "Hangul" in non_latin
    if is_japanese:
        languages.append("ja")
    if has_hangul:
        languages.append("ko")
    # Han characters inside Japanese (kanji) or Korean (hanja) text are not
    # evidence of Chinese. Treating them as Chinese made every Korean page
    # with hanja look like a Chinese/Korean mixed page.
    if "Han" in non_latin and not is_japanese and not has_hangul:
        languages.append("zh")
    if "Cyrillic" in non_latin:
        languages.append("uk" if re.search(r"[іїєґІЇЄҐ]", raw) else "ru")
    if "Arabic" in non_latin:
        if re.search(r"[ٹڈڑںھ]", raw):
            languages.append("ur")
        elif re.search(r"[پچژگ]", raw):
            languages.append("fa")
        else:
            languages.append("ar")
    for script, code in (("Devanagari", "hi"), ("Bengali", "bn"),
                         ("Thai", "th"), ("Tamil", "ta"), ("Telugu", "te"),
                         ("Greek", "el"), ("Hebrew", "he")):
        if script in non_latin:
            languages.append(code)
    languages = list(dict.fromkeys(languages))

    latin_letters = counts.get("Latin", 0)
    latin_share = latin_letters / total_letters
    non_latin_share = sum(non_latin.values()) / total_letters
    # Raw character share is a poor dominance measure across scripts: one Han
    # character carries about as much content as a 3-4 letter Latin word. A
    # Chinese page with a few English product terms must not be reported as an
    # English page, so dominance uses semantic units instead of characters.
    units = {script: count / _CHARS_PER_UNIT.get(script, _CHARS_PER_UNIT["Latin"])
             for script, count in counts.items()}
    total_units = sum(units.values())
    latin_units_share = units.get("Latin", 0.0) / total_units
    non_latin_units_share = sum(value for script, value in units.items()
                                if script != "Latin") / total_units
    if latin_letters >= _LATIN_MIN_LETTERS:
        latin_code, latin_confidence, latin_evidence = _latin_language_evidence(raw)
    else:
        latin_code, latin_confidence, latin_evidence = "und", "unknown", []
    latin_content_tokens = {token for token in _latin_tokens(raw) if len(token) >= 3}

    mixed = False
    mixed_languages: list[str] = []
    if len(languages) > 1:
        # Two substantive non-Latin scripts: report the ambiguity, do not
        # arbitrarily pick one as the page language.
        primary, confidence = "mul", "mixed-script-or-language"
        mixed = True
        mixed_languages = list(languages)
    elif languages:
        dominant = languages[0]
        if (latin_code != "und" and latin_units_share > 0.6
                and non_latin_units_share < 0.35):
            # The page is mostly Latin prose that happens to contain a few
            # characters of another script; the Latin language is dominant.
            primary, confidence = latin_code, latin_confidence
            languages = [latin_code, dominant]
            mixed = True
            mixed_languages = [latin_code, dominant]
        else:
            primary = dominant
            mixed = bool(latin_letters and latin_units_share >= 0.12
                         and len(latin_content_tokens) >= 4)
            if mixed:
                languages = [dominant] + ([latin_code] if latin_code != "und" else [])
                mixed_languages = list(languages)
                confidence = "mixed-script"
            else:
                confidence = "script"
    elif latin_code != "und":
        primary, confidence = latin_code, latin_confidence
        languages = [latin_code]
    else:
        primary, confidence = "und", "ambiguous"

    script_shares = {script: round(count / total_letters, 4)
                     for script, count in sorted(counts.items())}
    script_unit_shares = {script: round(value / total_units, 4)
                          for script, value in sorted(units.items())}
    uncertain = primary == "und" or confidence in {"ambiguous", "unknown"}
    return {"primary": primary, "languages": languages,
            "scripts": sorted(counts), "confidence": confidence,
            "detector": LANGUAGE_DETECTOR_VERSION,
            "mixed": mixed, "mixed_languages": mixed_languages,
            "script_shares": script_shares,
            "script_unit_shares": script_unit_shares,
            "lexical_evidence": latin_evidence,
            "uncertain": uncertain}


def is_rtl_language(code: str) -> bool:
    """Return True if language code uses right-to-left writing script."""
    meta = LANGUAGE_SUPPORT_MATRIX.get(code) or {}
    return meta.get("direction") == "rtl" or code in ("ar", "ur", "fa", "he")


def is_cjk_language(code: str) -> bool:
    """Return True if language code uses CJK/East Asian ideographic script."""
    return code in ("zh", "ja", "ko")


def get_support_matrix() -> dict:
    """Return complete public demographic and layer breakdown of language matrix."""
    counts = {"FULL_SUPPORT": 0, "PARTIAL_SUPPORT": 0, "PENDING_VERIFICATION": 0}
    for item in LANGUAGE_SUPPORT_MATRIX.values():
        counts[item["support_status"]] = counts.get(item["support_status"], 0) + 1
    acceptance_complete = all(
        LANGUAGE_SUPPORT_MATRIX[code]["support_status"] == "FULL_SUPPORT"
        and all(value == "VERIFIED"
                for value in LANGUAGE_SUPPORT_MATRIX[code]["layers"].values())
        for code in CURRENT_LANGUAGE_ACCEPTANCE_CODES
    )
    return {
        "source": POPULATION_DATA_SOURCE,
        "population_threshold_millions": 20,
        "summary": counts,
        "version_acceptance_scope": {
            "scope_id": CURRENT_LANGUAGE_ACCEPTANCE_SCOPE_ID,
            "language_codes": list(CURRENT_LANGUAGE_ACCEPTANCE_CODES),
            "language_count": len(CURRENT_LANGUAGE_ACCEPTANCE_CODES),
            "all_layers_verified": acceptance_complete,
            "out_of_scope_language_hints": list(OUT_OF_SCOPE_LANGUAGE_HINT_CODES),
            "worldwide_threshold_list_exhaustive": False,
        },
        "languages": LANGUAGE_SUPPORT_MATRIX,
    }


def get_language_support_level(code: str) -> dict:
    """Return detailed capability and layer breakdown for a given language code."""
    entry = LANGUAGE_SUPPORT_MATRIX.get(code)
    if not entry:
        return {
            "code": code,
            "supported": False,
            "support_status": "UNKNOWN",
            "note": "Language not in standard >20M demographic support matrix; degrades gracefully to neutral visual extraction.",
        }
    return {
        "code": code,
        "supported": entry["support_status"] != "PENDING_VERIFICATION",
        "support_status": entry["support_status"],
        "name": entry["name"],
        "script": entry["script"],
        "direction": entry["direction"],
        "layers": entry["layers"],
        "verified_evidence": entry.get("verified_evidence", []),
    }


# Standard Canonical Normalization Registry for Cross-Language Synonyms
# Ordered such that specific subscenes (e.g. checkout, receipt, worker) take precedence over general environment terms.
_CANONICAL_SCENE_REGISTRY = [
    # 1. Consumer sub-scenes
    ("consumer.receipt-still-life", ("小票", "发票", "账单", "receipt", "invoice", "bill", "factura", "recibo", "فاتورة", "रसीद")),
    ("consumer.checkout-payment", ("收银", "结算", "扫码支付", "买单", "checkout", "payment", "cashier", "pay", "caja", "pago", "الدفع", "भुगतान")),
    ("consumer.supermarket-shopping", ("选购", "推车", "货架通道", "shopping", "groceries", "aisle", "超市", "购物", "supermercado", "مستهلك", "سوبرماركت", "सुपरमार्केट")),

    # 2. Healthcare sub-scenes
    ("healthcare.radiologist-ct-scan", ("放射科", "阅片", "ct影像", "核磁", "ct scan", "mri", "radiolog", "radiology", "radiología", "الأشعة", "सीटी स्कैन")),
    ("healthcare.doctor-consultation", ("问诊", "面对面沟通", "诊室", "doctor consultation", "physician", "patient consultation", "consulta médica", "استشارة الطبيب", "डॉक्टर परामर्श")),
    ("healthcare.hospital-care", ("病房", "病床", "巡视", "护士", "hospital care", "ward", "clinic", "医院", "门诊", "hospital", "مستشفى", "अस्पताल")),

    # 3. Industry & production sub-scenes
    ("industry.factory-worker", ("车间装配工人", "装配工人", "工位工人", "手工校准", "factory worker", "assembler", "production worker", "工人", "车间工人", "operario", "عامل المصنع", "मजदूर")),
    ("industry.factory-production-line", ("生产线", "流水线", "自动化流水线", "总装车间", "factory", "production line", "assembly line", "fábrica", "خط الإنتاج", "कारखाना")),

    # 4. Delivery
    ("delivery.courier", ("外卖", "骑手", "配送", "快递", "courier", "rider", "delivery", "food delivery", "repartidor", "توصيل", "डिलीवरी")),

    # 5. Travel & transport
    ("transit.metro-commute", ("地铁", "通勤", "地铁站", "subway", "metro", "commute", "train", "مترو", "मेट्रो")),
    ("transit.air-travel", ("机场", "航班", "航站楼", "登机", "airport", "airplane", "flight", "departure hall", "aeropuerto", "مطار", "हवाई यात्रा")),
]


def canonical_scene_identity(topic: str, subject: str, action: str,
                             setting: str = "", *, explicit_canonical_id: str | None = None) -> str:
    """Resolve an open-vocabulary semantic scene to a stable, deduplicating canonical identity.

    Cross-language equivalent descriptions map to the same canonical identity.
    Distinct sub-scenes within the same broad topic retain distinct identities.
    """
    if explicit_canonical_id:
        cleaned = re.sub(r"[^a-z0-9_.-]+", "_", str(explicit_canonical_id).lower()).strip("_")
        if cleaned:
            return cleaned

    combined = f"{topic} {subject} {action} {setting}".lower()
    for canonical_id, triggers in _CANONICAL_SCENE_REGISTRY:
        if any(trigger.lower() in combined for trigger in triggers):
            return canonical_id

    # Open-vocabulary fallback must be collision-safe for scripts that do not
    # transliterate to ASCII. The previous ``scene.general`` fallback made
    # every unregistered Chinese, Bengali, Arabic, etc. scene share one ID,
    # which could silently collapse unrelated scenes into one photo. Keep a
    # readable ASCII prefix when available, then bind the complete normalized
    # source-language identity to a stable digest. Unknown translations are
    # deliberately not merged here: only the model's explicit canonical ID or
    # a registry match may claim cross-language equivalence.
    def normalize_identity_part(value: object) -> str:
        text = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", text,
                                           flags=re.UNICODE)).strip()

    identity_parts = [normalize_identity_part(value)
                      for value in (topic, subject, action, setting)]
    identity_payload = json.dumps(identity_parts, ensure_ascii=False,
                                  separators=(",", ":"))
    identity_hash = hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()[:20]

    topic_ascii = re.sub(r"[^a-z0-9]+", "", unicodedata.normalize(
        "NFKD", str(topic or "")).encode("ascii", "ignore").decode("ascii").casefold())
    readable_topic = topic_ascii[:18] or "scene"
    subject_action = " ".join(filter(None, (
        normalize_identity_part(subject), normalize_identity_part(action))))
    subject_action_ascii = unicodedata.normalize(
        "NFKD", subject_action).encode("ascii", "ignore").decode("ascii").casefold()
    readable_scene = re.sub(r"[^a-z0-9]+", "-", subject_action_ascii).strip("-")[:28]
    if readable_scene:
        return f"{readable_topic}.{readable_scene}.{identity_hash}"
    return f"{readable_topic}.scene.{identity_hash}"


def language_support_record(pages: list[dict]) -> dict:
    page_hints = [{"page": int(page["page"]),
                   "primary": (page.get("language_hint") or {}).get("primary", "und"),
                   "languages": list((page.get("language_hint") or {}).get("languages", [])),
                   "scripts": list((page.get("language_hint") or {}).get("scripts", [])),
                   "confidence": (page.get("language_hint") or {}).get("confidence", "unknown"),
                   "mixed": bool((page.get("language_hint") or {}).get("mixed")),
                   "uncertain": bool((page.get("language_hint") or {}).get("uncertain"))}
                  for page in pages]
    detected = sorted({code for row in page_hints for code in row["languages"]
                       if code != "und"})
    matrix = get_support_matrix()
    return {
        "detector": LANGUAGE_DETECTOR_VERSION,
        "detected_source_languages": detected,
        "source_languages_by_page": page_hints,
        # Pages whose language could not be established (numbers, emoji, bare
        # links, very short labels) and pages carrying two real languages are
        # listed explicitly so an acceptance review can see what remains
        # pending instead of reading a silent default.
        "mixed_language_pages": [row["page"] for row in page_hints if row["mixed"]],
        "uncertain_language_pages": [row["page"] for row in page_hints if row["uncertain"]],
        "pending_language_handling": (
            "uncertain pages are still planned from their original text; the planner "
            "receives the source text and the und/mixed hint instead of a forced guess"
        ),
        "scene_fields_language": "source page language; preserve original wording",
        "photo_query_language": "en",
        "source_text_policy": "source text and evidence remain local; only neutral English visual queries reach photo providers",
        "language_hint_is_advisory": True,
        "support_matrix_source": POPULATION_DATA_SOURCE,
        "support_matrix_summary": matrix["summary"],
    }
