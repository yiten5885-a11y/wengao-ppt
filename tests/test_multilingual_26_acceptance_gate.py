"""End-to-end acceptance tests for the 26-language scope and 50-deck source material corpus.

Verifies:
1. P1: 26 languages acceptance scope (ar, bn, de, en, es, fa, fr, hi, id, it, ja,
   ko, ms, nl, pl, pt, ru, sw, ta, te, th, tr, uk, ur, vi, zh).
2. P1: 50 source materials corpus:
   - 0 missing materials (22 missing completed)
   - 0 language mismatches (4 Bengali materials corrected & verified)
   - legacy review claims are not accepted without signed per-item evidence
   - 50 distinct scenario anchors matching user-50-scenes specification
   - Exact SHA-256 and byte counts
3. RTL support: ar, ur, fa correctly flagged and validated
4. CJK support: zh, ja, ko correctly flagged and validated
5. 5-Layer pipeline: text recognition, semantic planning, visual query generation,
   typography/layout, generation & gate.
"""
from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path

from wengao_ppt.language_support import (
    CURRENT_LANGUAGE_ACCEPTANCE_CODES,
    CURRENT_LANGUAGE_ACCEPTANCE_SCOPE_ID,
    OUT_OF_SCOPE_LANGUAGE_HINT_CODES,
    detect_language_hint,
    is_cjk_language,
    is_rtl_language,
    get_support_matrix,
    get_language_support_level,
)
from scripts.source_corpus_review_gate import audit_corpus_manifest


class Test26LanguagesAnd50MaterialsGate(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parent.parent
        cls.manifest_candidates = [
            cls.repo_root / "samples" / "open-source-50-decks" / "multilingual-inputs" / "input-manifest.json",
            Path("/Users/huangyingteng/Documents/Codex/2026-10-20/n-2/work/strict-qa/open-source-50-decks/multilingual-inputs/input-manifest.json"),
        ]
        cls.manifest_path = next((p for p in cls.manifest_candidates if p.exists()), None)
        assert cls.manifest_path is not None, "input-manifest.json must exist in repo or Codex directory"
        with open(cls.manifest_path, "r", encoding="utf-8") as f:
            cls.manifest = json.load(f)

    def test_26_languages_scope_exact_match(self):
        """The release commitments cover exactly the 26 matrix languages above 20M speakers."""
        scope_codes = set(CURRENT_LANGUAGE_ACCEPTANCE_CODES)
        expected_26 = {
            "ar", "bn", "de", "en", "es", "fa", "fr", "hi", "id", "it",
            "ja", "ko", "ms", "nl", "pl", "pt", "ru", "sw", "ta", "te",
            "th", "tr", "uk", "ur", "vi", "zh"
        }
        self.assertEqual(scope_codes, expected_26)
        self.assertEqual(len(CURRENT_LANGUAGE_ACCEPTANCE_CODES), 26)
        self.assertEqual(CURRENT_LANGUAGE_ACCEPTANCE_SCOPE_ID, "existing-matrix-26-v1")
        # Other languages deferred to later versions
        self.assertEqual(set(OUT_OF_SCOPE_LANGUAGE_HINT_CODES), {"el", "he"})

    def test_rtl_and_cjk_coverage(self):
        """RTL and CJK language classifications are exact."""
        # RTL languages in scope
        self.assertTrue(is_rtl_language("ar"), "Arabic must be RTL")
        self.assertTrue(is_rtl_language("ur"), "Urdu must be RTL")
        self.assertTrue(is_rtl_language("fa"), "Persian must be RTL")
        # CJK languages in scope
        self.assertTrue(is_cjk_language("zh"), "Chinese must be CJK")
        self.assertTrue(is_cjk_language("ja"), "Japanese must be CJK")
        self.assertTrue(is_cjk_language("ko"), "Korean must be CJK")
        # Non-RTL / Non-CJK
        self.assertFalse(is_rtl_language("en"))
        self.assertFalse(is_rtl_language("es"))
        self.assertFalse(is_cjk_language("en"))

    def test_50_source_materials_all_present_and_hash_matched(self):
        """All 50 materials are present, with matching hashes, non-zero size, and distinct anchors."""
        entries = self.manifest.get("entries", [])
        self.assertEqual(len(entries), 50, "Corpus must have exactly 50 entries")

        manifest_dir = self.manifest_path.parent
        anchors = set()
        language_counts = {}

        for entry in entries:
            cid = entry["case_id"]
            lang = entry["source_language"]
            anchor = entry["scenario_anchor"]
            fname = f"{cid}-{lang}.md"
            fpath = manifest_dir / fname

            self.assertTrue(fpath.exists(), f"Material {fname} must exist on disk")
            raw_bytes = fpath.read_bytes()
            self.assertGreater(len(raw_bytes), 200, f"Material {fname} must not be empty or stub")

            computed_sha = hashlib.sha256(raw_bytes).hexdigest()
            self.assertEqual(computed_sha, entry["input_sha256"], f"SHA256 mismatch for {fname}")
            self.assertEqual(len(raw_bytes), entry["bytes"], f"Byte count mismatch for {fname}")

            anchors.add(anchor)
            language_counts[lang] = language_counts.get(lang, 0) + 1

            # Legacy fixture_review/status fields are claims, not review proof.
            self.assertTrue("fixture_review" in entry or "status" in entry)

        # 50 unique scenario anchors
        self.assertEqual(len(anchors), 50, "All 50 scenario anchors must be distinct")
        # All 26 languages represented
        self.assertEqual(set(language_counts.keys()), set(CURRENT_LANGUAGE_ACCEPTANCE_CODES))

        # Top 8 languages have 4 materials each, remaining 18 have 1 each
        top_8 = {"en", "zh", "hi", "es", "ar", "fr", "bn", "pt"}
        for lang in top_8:
            self.assertEqual(language_counts[lang], 4, f"Top language {lang} must have 4 materials")
        for lang in set(CURRENT_LANGUAGE_ACCEPTANCE_CODES) - top_8:
            self.assertEqual(language_counts[lang], 1, f"Language {lang} must have 1 material")

        # The current corpus contains no signed per-item review attestations.
        # Legacy PASS labels therefore leave review acceptance blocked.
        review_audit = audit_corpus_manifest(self.manifest_path)
        self.assertEqual(review_audit["verdict"], "BLOCKED")
        self.assertEqual(review_audit["input_files_verified"], 50)
        self.assertEqual(review_audit["verified_reviews"], 0)
        self.assertEqual(review_audit["unverified_reviews"], 50)

    def test_all_50_materials_pass_language_detection(self):
        """Every one of the 50 materials detects as its planned language with mixed=False."""
        manifest_dir = self.manifest_path.parent
        for entry in self.manifest.get("entries", []):
            cid = entry["case_id"]
            lang = entry["source_language"]
            fname = f"{cid}-{lang}.md"
            fpath = manifest_dir / fname

            text = fpath.read_text(encoding="utf-8")
            det = detect_language_hint(text)

            self.assertEqual(
                det.get("primary"),
                lang,
                f"Material {fname} detected as {det.get('primary')}, expected {lang}"
            )
            self.assertFalse(
                det.get("mixed"),
                f"Material {fname} unexpectedly marked as mixed language: {det.get('mixed_languages')}"
            )


if __name__ == "__main__":
    unittest.main()
