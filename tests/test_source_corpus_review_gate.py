from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from scripts.source_corpus_review_gate import (
    audit_corpus_manifest,
    canonical_attestation_bytes,
)


ROOT = Path(__file__).resolve().parent.parent
CURRENT_MANIFEST = ROOT / "samples/open-source-50-decks/multilingual-inputs/input-manifest.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_current_50_self_reported_passes_are_not_review_evidence():
    report = audit_corpus_manifest(CURRENT_MANIFEST)
    assert report["verdict"] == "BLOCKED"
    assert report["input_files_verified"] == 50
    assert report["verified_reviews"] == 0
    assert report["unverified_reviews"] == 50
    assert report["legacy_claims_used"] is False
    assert any(x["code"] == "TRUSTED_REVIEWER_ROSTER_NOT_PINNED"
               for x in report["findings"])
    assert sum(x["code"] == "REVIEW_ATTESTATION_MISSING"
               for x in report["findings"]) == 50


def test_attestation_requires_trusted_roster_signature_and_source_binding(tmp_path: Path):
    openssl = shutil.which("openssl")
    if not openssl:
        return
    corpus = tmp_path / "corpus"
    evidence = corpus / "reviews"
    keys = tmp_path / "trusted"
    evidence.mkdir(parents=True)
    keys.mkdir()

    source = corpus / "L01-en.md"
    source.write_text("A reviewed English source describing a worker in a factory.\n",
                      encoding="utf-8")
    source_sha = _sha(source)
    private_key = keys / "reviewer-private.pem"
    public_key = keys / "reviewer-public.pem"
    subprocess.run([openssl, "genpkey", "-algorithm", "RSA", "-pkeyopt",
                    "rsa_keygen_bits:2048", "-out", str(private_key)],
                   check=True, capture_output=True, timeout=15)
    subprocess.run([openssl, "pkey", "-in", str(private_key), "-pubout",
                    "-out", str(public_key)],
                   check=True, capture_output=True, timeout=10)

    roster = keys / "reviewers.json"
    roster.write_text(json.dumps({
        "schema_version": 1,
        "reviewers": [{
            "reviewer_id": "reviewer-en-01",
            "active": True,
            "qualified_languages": ["en"],
            "public_key_path": "reviewer-public.pem",
            "public_key_sha256": _sha(public_key),
        }],
    }, ensure_ascii=False), encoding="utf-8")

    attestation = {
        "schema": "source-corpus-review-attestation/v1",
        "case_id": "L01",
        "source_language": "en",
        "source_sha256": source_sha,
        "verdict": "PASS",
        "reviewer_id": "reviewer-en-01",
        "reviewed_at": "2026-10-23T12:00:00+00:00",
        "criteria": {
            "native_language_and_grammar": "PASS",
            "facts_and_numbers_preserved": "PASS",
            "scene_anchor_grounded": "PASS",
            "topic_distinctness": "PASS",
        },
        "review_notes": "Verified language, facts, scene relevance, and distinct topic.",
        "issue_log": [],
    }
    payload = evidence / "L01-en.canonical.json"
    payload.write_bytes(canonical_attestation_bytes(attestation))
    signature = evidence / "L01-en.sig"
    subprocess.run([openssl, "dgst", "-sha256", "-sign", str(private_key),
                    "-out", str(signature), str(payload)],
                   check=True, capture_output=True, timeout=10)
    attestation["signature"] = {
        "algorithm": "RSA-SHA256",
        "path": "reviews/L01-en.sig",
        "sha256": _sha(signature),
    }
    att_file = evidence / "L01-en.json"
    att_file.write_text(json.dumps(attestation, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    manifest = corpus / "input-manifest.json"
    manifest.write_text(json.dumps({"entries": [{
        "case_id": "L01",
        "source_language": "en",
        "source_file": "L01-en.md",
        "scenario_anchor": "factory-worker",
        "input_sha256": source_sha,
        "bytes": source.stat().st_size,
        "review_attestation": {
            "path": "reviews/L01-en.json",
            "sha256": _sha(att_file),
        },
        # Legacy claims must be irrelevant even when they say PASS.
        "fixture_review": {"verdict": "PASS"},
        "status": "REVIEWED_AND_VERIFIED",
    }]}, ensure_ascii=False), encoding="utf-8")

    report = audit_corpus_manifest(
        manifest, reviewer_roster_path=roster,
        trusted_roster_sha256=_sha(roster), expected_count=1,
    )
    assert report["verdict"] == "PASS"
    assert report["verified_reviews"] == 1

    # Tampering with the review after signing must invalidate it.
    attestation["review_notes"] = "Forged PASS after the reviewer signed the record."
    att_file.write_text(json.dumps(attestation, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    entry = json.loads(manifest.read_text(encoding="utf-8"))["entries"][0]
    entry["review_attestation"]["sha256"] = _sha(att_file)
    manifest.write_text(json.dumps({"entries": [entry]}, ensure_ascii=False),
                        encoding="utf-8")
    report = audit_corpus_manifest(
        manifest, reviewer_roster_path=roster,
        trusted_roster_sha256=_sha(roster), expected_count=1,
    )
    assert report["verdict"] == "BLOCKED"
    assert any(x["code"] == "REVIEW_SIGNATURE_INVALID" for x in report["findings"])
