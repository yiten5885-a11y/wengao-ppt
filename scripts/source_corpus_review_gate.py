"""Fail-closed evidence verifier for the 50 multilingual source materials.

Legacy manifest fields such as ``fixture_review.verdict`` and ``status`` are
descriptive claims only. They are never used as review evidence here. A review
counts only when a detached RSA-SHA256 signature verifies against a separately
trusted reviewer roster whose SHA-256 is pinned by the caller.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA = "source-corpus-review-attestation/v1"
REQUIRED_CRITERIA = {
    "native_language_and_grammar",
    "facts_and_numbers_preserved",
    "scene_anchor_grounded",
    "topic_distinctness",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_attestation_bytes(attestation: dict[str, Any]) -> bytes:
    """Return the signed bytes: canonical JSON excluding the detached signature."""
    payload = {k: v for k, v in attestation.items() if k != "signature"}
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _safe_ref(base: Path, ref: Any, label: str, findings: list[dict[str, Any]]) -> Path | None:
    if not isinstance(ref, dict):
        findings.append({"code": "EVIDENCE_REFERENCE_MISSING", "item": label})
        return None
    rel = ref.get("path")
    expected = ref.get("sha256")
    if not isinstance(rel, str) or not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
        findings.append({"code": "EVIDENCE_PATH_INVALID", "item": label})
        return None
    if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
        findings.append({"code": "EVIDENCE_HASH_INVALID", "item": label})
        return None
    try:
        resolved_base = base.resolve(strict=True)
        path = (resolved_base / rel).resolve(strict=True)
    except OSError:
        findings.append({"code": "EVIDENCE_FILE_MISSING", "item": label})
        return None
    if not path.is_relative_to(resolved_base) or not path.is_file():
        findings.append({"code": "EVIDENCE_PATH_OUTSIDE_ROOT", "item": label})
        return None
    actual = sha256_file(path)
    if actual != expected:
        findings.append({"code": "EVIDENCE_HASH_MISMATCH", "item": label,
                         "expected": expected, "actual": actual})
        return None
    return path


def _load_json(path: Path, label: str, findings: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        findings.append({"code": "EVIDENCE_JSON_INVALID", "item": label})
        return None
    if not isinstance(value, dict):
        findings.append({"code": "EVIDENCE_JSON_INVALID", "item": label})
        return None
    return value


def _verify_signature(public_key: Path, signature: Path, payload: bytes) -> bool:
    openssl = shutil.which("openssl")
    if not openssl:
        return False
    with tempfile.NamedTemporaryFile(prefix="wengao-review-payload-") as payload_file:
        payload_file.write(payload)
        payload_file.flush()
        try:
            result = subprocess.run(
                [openssl, "dgst", "-sha256", "-verify", str(public_key),
                 "-signature", str(signature), payload_file.name],
                capture_output=True, text=True, timeout=8, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
    return result.returncode == 0 and "Verified OK" in result.stdout


def _trusted_roster(
    roster_path: Path | None,
    expected_roster_sha256: str | None,
    manifest_dir: Path,
    findings: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if roster_path is None or not expected_roster_sha256:
        findings.append({"code": "TRUSTED_REVIEWER_ROSTER_NOT_PINNED"})
        return {}
    if not SHA256_RE.fullmatch(expected_roster_sha256):
        findings.append({"code": "TRUSTED_REVIEWER_ROSTER_HASH_INVALID"})
        return {}
    try:
        roster_path = roster_path.resolve(strict=True)
        if roster_path.is_relative_to(manifest_dir.resolve(strict=True)):
            findings.append({"code": "TRUSTED_REVIEWER_ROSTER_NOT_EXTERNAL"})
            return {}
        actual = sha256_file(roster_path)
    except OSError:
        findings.append({"code": "TRUSTED_REVIEWER_ROSTER_MISSING"})
        return {}
    if actual != expected_roster_sha256:
        findings.append({"code": "TRUSTED_REVIEWER_ROSTER_HASH_MISMATCH",
                         "expected": expected_roster_sha256, "actual": actual})
        return {}
    roster = _load_json(roster_path, "trusted_reviewer_roster", findings)
    if not roster or roster.get("schema_version") != 1:
        findings.append({"code": "TRUSTED_REVIEWER_ROSTER_INVALID"})
        return {}
    reviewers = roster.get("reviewers")
    if not isinstance(reviewers, list):
        findings.append({"code": "TRUSTED_REVIEWER_ROSTER_INVALID"})
        return {}
    result: dict[str, dict[str, Any]] = {}
    for reviewer in reviewers:
        if not isinstance(reviewer, dict):
            continue
        reviewer_id = reviewer.get("reviewer_id")
        if isinstance(reviewer_id, str) and reviewer_id and reviewer_id not in result:
            result[reviewer_id] = reviewer
    return result


def _manifest_source_path(manifest_dir: Path, entry: dict[str, Any]) -> Path | None:
    # source_path in the legacy manifest points to a different workspace. Use
    # an explicit source_file when available, otherwise the corpus-local name.
    rel = entry.get("source_file")
    if not isinstance(rel, str) or not rel:
        rel = f"{entry.get('case_id', '')}-{entry.get('source_language', '')}.md"
    if Path(rel).is_absolute() or ".." in Path(rel).parts:
        return None
    try:
        root = manifest_dir.resolve(strict=True)
        path = (root / rel).resolve(strict=True)
    except OSError:
        return None
    return path if path.is_relative_to(root) and path.is_file() else None


def _verify_attestation(
    entry: dict[str, Any],
    source_sha: str,
    manifest_dir: Path,
    reviewers: dict[str, dict[str, Any]],
    findings: list[dict[str, Any]],
) -> bool:
    case_id = str(entry.get("case_id", "?"))
    att_path = _safe_ref(manifest_dir, entry.get("review_attestation"),
                         f"{case_id}.review_attestation", findings)
    if att_path is None:
        findings.append({"code": "REVIEW_ATTESTATION_MISSING", "case_id": case_id})
        return False
    att = _load_json(att_path, f"{case_id}.review_attestation", findings)
    if att is None:
        return False
    language = entry.get("source_language")
    expected = {
        "schema": SCHEMA,
        "case_id": case_id,
        "source_language": language,
        "source_sha256": source_sha,
        "verdict": "PASS",
    }
    for key, value in expected.items():
        if att.get(key) != value:
            findings.append({"code": "REVIEW_BINDING_MISMATCH", "case_id": case_id,
                             "field": key})
    try:
        reviewed_at = datetime.fromisoformat(str(att.get("reviewed_at", "")).replace("Z", "+00:00"))
        if reviewed_at.tzinfo is None:
            raise ValueError("timezone required")
    except ValueError:
        findings.append({"code": "REVIEW_TIMESTAMP_INVALID", "case_id": case_id})
    criteria = att.get("criteria")
    if not isinstance(criteria, dict) or not REQUIRED_CRITERIA.issubset(criteria) or any(
        criteria.get(k) != "PASS" for k in REQUIRED_CRITERIA
    ):
        findings.append({"code": "REVIEW_CRITERIA_INCOMPLETE", "case_id": case_id})
    notes = att.get("review_notes")
    issue_log = att.get("issue_log")
    if not isinstance(notes, str) or len(notes.strip()) < 20 or not isinstance(issue_log, list):
        findings.append({"code": "REVIEW_DETAIL_INCOMPLETE", "case_id": case_id})

    reviewer_id = att.get("reviewer_id")
    reviewer = reviewers.get(reviewer_id) if isinstance(reviewer_id, str) else None
    if not reviewer or reviewer.get("active") is not True:
        findings.append({"code": "REVIEWER_NOT_TRUSTED", "case_id": case_id})
        return False
    qualified = reviewer.get("qualified_languages")
    if not isinstance(qualified, list) or (language not in qualified and "*" not in qualified):
        findings.append({"code": "REVIEWER_NOT_QUALIFIED_FOR_LANGUAGE", "case_id": case_id})

    key_path = reviewer.get("public_key_path")
    key_sha = reviewer.get("public_key_sha256")
    key_file = _safe_ref(roster_path := Path(reviewer.get("_roster_dir", manifest_dir)),
                         {"path": key_path, "sha256": key_sha},
                         f"{case_id}.reviewer_public_key", findings)
    signature_record = att.get("signature")
    if not isinstance(signature_record, dict) or signature_record.get("algorithm") != "RSA-SHA256":
        findings.append({"code": "REVIEW_SIGNATURE_ALGORITHM_INVALID", "case_id": case_id})
        return False
    signature = _safe_ref(manifest_dir, signature_record,
                          f"{case_id}.review_signature", findings)
    if key_file is None or signature is None:
        return False
    if not _verify_signature(key_file, signature, canonical_attestation_bytes(att)):
        findings.append({"code": "REVIEW_SIGNATURE_INVALID", "case_id": case_id})
        return False
    return not any(f.get("case_id") == case_id and f.get("code", "").startswith("REVIEW_")
                   for f in findings)


def audit_corpus_manifest(
    manifest_path: str | Path,
    *,
    reviewer_roster_path: str | Path | None = None,
    trusted_roster_sha256: str | None = None,
    expected_count: int = 50,
) -> dict[str, Any]:
    """Verify source bytes and qualified, signed reviews; never trust legacy PASS fields."""
    findings: list[dict[str, Any]] = []
    manifest_path = Path(manifest_path)
    try:
        manifest_path = manifest_path.resolve(strict=True)
    except OSError:
        return {"verdict": "BLOCKED", "expected_count": expected_count,
                "input_files_verified": 0, "verified_reviews": 0,
                "unverified_reviews": expected_count,
                "findings": [{"code": "MANIFEST_MISSING"}]}
    manifest = _load_json(manifest_path, "input_manifest", findings)
    if manifest is None:
        return {"verdict": "BLOCKED", "expected_count": expected_count,
                "input_files_verified": 0, "verified_reviews": 0,
                "unverified_reviews": expected_count, "findings": findings}
    entries = manifest.get("entries")
    if not isinstance(entries, list) or len(entries) != expected_count:
        findings.append({"code": "CORPUS_ENTRY_COUNT_MISMATCH",
                         "expected": expected_count,
                         "actual": len(entries) if isinstance(entries, list) else None})
        entries = entries if isinstance(entries, list) else []
    roster_path = Path(reviewer_roster_path) if reviewer_roster_path else None
    reviewers = _trusted_roster(roster_path, trusted_roster_sha256,
                                manifest_path.parent, findings)
    for reviewer in reviewers.values():
        reviewer["_roster_dir"] = roster_path.parent if roster_path else manifest_path.parent

    manifest_dir = manifest_path.parent
    seen_ids: set[str] = set()
    seen_anchors: set[str] = set()
    input_verified = 0
    review_verified = 0
    review_unverified = 0
    for entry in entries:
        if not isinstance(entry, dict):
            findings.append({"code": "CORPUS_ENTRY_INVALID"})
            review_unverified += 1
            continue
        case_id = entry.get("case_id")
        language = entry.get("source_language")
        anchor = entry.get("scenario_anchor")
        if not isinstance(case_id, str) or not case_id or case_id in seen_ids:
            findings.append({"code": "CORPUS_CASE_ID_INVALID_OR_DUPLICATE",
                             "case_id": case_id})
        else:
            seen_ids.add(case_id)
        if not isinstance(anchor, str) or not anchor or anchor in seen_anchors:
            findings.append({"code": "SCENARIO_ANCHOR_INVALID_OR_DUPLICATE",
                             "case_id": case_id})
        else:
            seen_anchors.add(anchor)
        source = _manifest_source_path(manifest_dir, entry)
        if source is None:
            findings.append({"code": "SOURCE_FILE_MISSING_OR_OUTSIDE_CORPUS",
                             "case_id": case_id})
            review_unverified += 1
            continue
        raw_sha = sha256_file(source)
        raw_size = source.stat().st_size
        if raw_sha != entry.get("input_sha256") or raw_size != entry.get("bytes"):
            findings.append({"code": "SOURCE_IDENTITY_MISMATCH", "case_id": case_id,
                             "actual_sha256": raw_sha, "actual_bytes": raw_size})
            review_unverified += 1
            continue
        input_verified += 1
        if _verify_attestation(entry, raw_sha, manifest_dir, reviewers, findings):
            review_verified += 1
        else:
            review_unverified += 1

    if len(seen_ids) != expected_count:
        findings.append({"code": "CORPUS_CASE_ID_COVERAGE_MISMATCH",
                         "expected": expected_count, "actual": len(seen_ids)})
    if len(seen_anchors) != expected_count:
        findings.append({"code": "CORPUS_ANCHOR_COVERAGE_MISMATCH",
                         "expected": expected_count, "actual": len(seen_anchors)})
    verdict = "PASS" if (
        len(entries) == expected_count and input_verified == expected_count
        and review_verified == expected_count and not findings
    ) else "BLOCKED"
    return {
        "schema": "source-corpus-review-audit/v1",
        "verdict": verdict,
        "expected_count": expected_count,
        "input_files_verified": input_verified,
        "verified_reviews": review_verified,
        "unverified_reviews": review_unverified,
        "legacy_claims_used": False,
        "reviewer_roster_sha256": trusted_roster_sha256,
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--reviewer-roster", type=Path,
                        help="trusted reviewer roster stored separately from the corpus")
    parser.add_argument("--trusted-roster-sha256",
                        help="roster SHA-256 pinned by the release owner/CI")
    args = parser.parse_args(argv)
    report = audit_corpus_manifest(
        args.manifest,
        reviewer_roster_path=args.reviewer_roster,
        trusted_roster_sha256=args.trusted_roster_sha256,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
