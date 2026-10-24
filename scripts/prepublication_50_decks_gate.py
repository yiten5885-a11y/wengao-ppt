"""Fail-closed release gate for the 50 pre-publication real decks (WP9).

Base contract ``contracts/prepublication_50_decks_gate_v1.json`` is consumed
read-only. The identity requirements enforced here live in
``contracts/prepublication_50_decks_identity_v1.json`` (owned by WP9).

Manifest fields are treated as *claims*. A deck counts only when its identity
can be recomputed from bytes and records actually present on disk:

* the input file and the generated PPTX are re-hashed,
* the number of really inserted scene images is recounted from the PPTX,
* the frozen code/material set is re-hashed into ``build_sha256``, and every
  deck must have been produced under exactly that build,
* the task run record, per-page previews, download artifact and review record
  must exist, hash-match and cover every page.

Everything is fail-closed: missing or unreadable evidence, wrong hashes,
duplicate identities, stale results, forged PASS, image-free decks,
self-reported image counts, missing previews/downloads and unrendered pages all
produce ``BLOCKED``. The gate never returns PASS on absent evidence.
"""
from __future__ import annotations

import argparse
import base64
import io
import hashlib
import json
import os
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE


SCHEMA = "prepublication-50-decks-gate-audit/v1"
DEFAULT_EXPECTED_COUNT = 50
REQUIRED_EXPECTED_COUNT = 50
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BUILD_ROOT = Path(__file__).resolve().parents[1]
TRUST_STORE_PATH = BUILD_ROOT / "contracts/prepublication_50_decks_trust_store_v1.json"
# The release gate pins the trust-store bytes. Provisioning keys changes this
# pin and therefore changes the frozen build identity; evidence cannot supply
# or replace its own trust roots.
TRUST_STORE_SHA256_PIN = "7b84b787e96f7aaa8db9836dbdd74cfe54ff340847f5df059c0ee273ba1f2ab7"
FREEZE_SCHEMA = "prepublication-50-decks-freeze/v1"
# These paths contain run outputs, local state, QA reports, or runtime caches;
# none is loaded as candidate code/material by the workbench. Every other regular
# file in the candidate root is part of the freeze, including assets, vendor code,
# contracts, samples, tests, launchers, and documentation.
FREEZE_EXCLUDED_DIRS = {
    ".git", ".venv", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "__pycache__", "node_modules", ".fontconfig", "jobs", "reports",
    "wb_loopback_data", "material_tasks", "work", "workbench_previews",
    "workbench_uploads",
}
FREEZE_EXCLUDED_PREFIXES = {
    "samples/open-source-50-decks/multilingual-inputs/generation-evidence",
}
FREEZE_EXCLUDED_SUFFIXES = {".log", ".pyc", ".pyo", ".tmp", ".sqlite", ".sqlite3", ".db"}
FREEZE_EXCLUDED_NAMES = {".DS_Store"}
SLIDE_RE = re.compile(r"ppt/slides/slide\d+\.xml")
IMAGE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp",
    ".emf", ".wmf", ".svg",
}
PASS_VERDICT_FIELDS = (
    "independent_content_fidelity_verdict",
    "layout_and_placement_safety_verdict",
    "visual_review_verdict",
    "candidate_gate_verdict",
)
FALSE_FLAGS = ("is_synthetic_fixture", "is_replay", "is_mock")
RUNTIME_ENVIRONMENT_FIELDS = (
    "os_name", "architecture", "python_implementation", "python_version",
    "dependency_manifest_sha256", "renderer_name", "renderer_version",
)
ALLOWED_IMAGE_PROVIDERS = {
    "openverse": {"CC0", "Public Domain Mark", "Public Domain", "CC BY 4.0"},
    "wikimedia_commons": {"CC0", "Public Domain Mark", "Public Domain", "CC BY 4.0"},
    "pexels": {"Pexels License"},
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA256_RE.fullmatch(value))


def _load_json(path: Path, label: str, findings: list[dict[str, Any]]) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        findings.append({"code": "EVIDENCE_JSON_INVALID", "item": label})
        return None
    if not isinstance(value, dict):
        findings.append({"code": "EVIDENCE_JSON_INVALID", "item": label})
        return None
    return value


def _resolve_rel(base: Path, rel: Any) -> Path | None:
    """Resolve a manifest-relative path safely; reject absolute and escaping paths."""
    if not isinstance(rel, str) or not rel:
        return None
    candidate = Path(rel)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    try:
        root = base.resolve(strict=True)
        path = (root / candidate).resolve(strict=True)
    except OSError:
        return None
    return path if path.is_relative_to(root) and path.is_file() else None


def _safe_ref(
    base: Path,
    ref: Any,
    label: str,
    findings: list[dict[str, Any]],
) -> Path | None:
    """Return the file only when it exists inside ``base`` and its hash matches."""
    if not isinstance(ref, dict):
        findings.append({"code": "EVIDENCE_REFERENCE_MISSING", "item": label})
        return None
    expected = ref.get("sha256")
    if not _is_sha256(expected):
        findings.append({"code": "EVIDENCE_HASH_INVALID", "item": label})
        return None
    path = _resolve_rel(base, ref.get("path"))
    if path is None:
        findings.append({"code": "EVIDENCE_FILE_MISSING", "item": label})
        return None
    actual = sha256_file(path)
    if actual != expected:
        findings.append({"code": "EVIDENCE_HASH_MISMATCH", "item": label,
                         "expected": expected, "actual": actual})
        return None
    return path


def _require(condition: bool, code: str, findings: list[dict[str, Any]], **extra: Any) -> bool:
    if not condition:
        finding = {"code": code}
        finding.update(extra)
        findings.append(finding)
    return condition


def _canonical_signed_bytes(document: dict[str, Any]) -> bytes:
    payload = {key: value for key, value in document.items() if key != "signature"}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _verify_ed25519_document(
    document: dict[str, Any],
    key_registry: dict[str, Any],
    label: str,
    findings: list[dict[str, Any]],
    *,
    expected_key_id: str | None = None,
) -> str | None:
    """Verify a signed record against a public key from the pinned trust store."""
    signature = document.get("signature")
    if not isinstance(signature, dict):
        findings.append({"code": "SIGNED_ATTESTATION_MISSING", "item": label})
        return None
    key_id = signature.get("key_id")
    encoded = signature.get("signature_base64")
    if not isinstance(key_id, str) or not key_id or not isinstance(encoded, str):
        findings.append({"code": "SIGNED_ATTESTATION_INVALID", "item": label})
        return None
    if expected_key_id is not None and key_id != expected_key_id:
        findings.append({"code": "SIGNED_ATTESTATION_KEY_MISMATCH", "item": label,
                         "expected": expected_key_id, "actual": key_id})
        return None
    key_entry = key_registry.get(key_id)
    if not isinstance(key_entry, dict) or key_entry.get("status") != "active":
        findings.append({"code": "SIGNED_ATTESTATION_KEY_UNTRUSTED", "item": label,
                         "key_id": key_id})
        return None
    try:
        public_key = base64.b64decode(key_entry["public_key_base64"], validate=True)
        signature_bytes = base64.b64decode(encoded, validate=True)
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature_bytes, _canonical_signed_bytes(document)
        )
    except (KeyError, ValueError, InvalidSignature, TypeError):
        findings.append({"code": "SIGNED_ATTESTATION_INVALID", "item": label,
                         "key_id": key_id})
        return None
    return key_id


def _load_pinned_trust_store(findings: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Load keys from the candidate-owned, byte-pinned trust root.

    Evidence manifests cannot point to their own trust store. The current
    candidate intentionally has no production keys provisioned, so release
    verification remains BLOCKED until the runtime and qualified reviewers
    receive separately managed keys and this source/build is refrozen.
    """
    if not TRUST_STORE_PATH.is_file():
        findings.append({"code": "TRUST_STORE_MISSING"})
        return None
    actual = sha256_file(TRUST_STORE_PATH)
    if actual != TRUST_STORE_SHA256_PIN:
        findings.append({"code": "TRUST_STORE_HASH_MISMATCH", "actual": actual,
                         "expected": TRUST_STORE_SHA256_PIN})
        return None
    store = _load_json(TRUST_STORE_PATH, "trusted_keys", findings)
    if store is None:
        return None
    if store.get("schema") != "prepublication-50-decks-trust-store/v1":
        findings.append({"code": "TRUST_STORE_SCHEMA_INVALID"})
        return None
    if store.get("status") != "PROVISIONED":
        findings.append({"code": "TRUST_STORE_UNPROVISIONED"})
        return None
    for role in ("freeze_signers", "runtime_signers", "reviewers"):
        if not isinstance(store.get(role), dict) or not store[role]:
            findings.append({"code": "TRUST_STORE_ROLE_EMPTY", "role": role})
    return store if not findings else None


def _validate_preview_png(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            if image.format != "PNG" or image.width < 1 or image.height < 1:
                return None
            size = image.size
            image.verify()
        return size
    except Exception:
        return None


def recompute_pptx_facts(pptx_path: Path) -> dict[str, int] | None:
    """Parse a real OOXML presentation and decode every inserted raster image.

    A ZIP containing ``ppt/slides/slide1.xml`` is not sufficient. python-pptx
    resolves the presentation/slide relationships; Pillow then verifies the
    bytes reached through each actual picture relationship.
    """
    try:
        presentation = Presentation(str(pptx_path))
        pages = len(presentation.slides)
        images: list[dict[str, Any]] = []
        for slide_number, slide in enumerate(presentation.slides, start=1):
            for shape in slide.shapes:
                if shape.shape_type != MSO_SHAPE_TYPE.PICTURE:
                    continue
                blob = shape.image.blob
                with Image.open(io.BytesIO(blob)) as image:
                    image.verify()
                with Image.open(io.BytesIO(blob)) as image:
                    image_format = image.format
                    width, height = image.size
                if image_format not in {"PNG", "JPEG", "WEBP", "GIF", "TIFF", "BMP"}:
                    return None
                images.append({
                    "slide": slide_number,
                    "shape_name": shape.name,
                    "sha256": sha256_bytes(blob),
                    "format": image_format,
                    "width": width,
                    "height": height,
                })
        with zipfile.ZipFile(pptx_path) as zf:
            media = sum(
                1 for name in zf.namelist()
                if name.startswith("ppt/media/")
                and not name.endswith("/")
                and Path(name).suffix.lower() in IMAGE_SUFFIXES
            )
    except Exception:
        return None
    return {"page_count": pages, "slide_pictures": len(images),
            "media_images": media, "images": images}


def compute_build_identity(
    root: Path,
    freeze: dict[str, Any],
    findings: list[dict[str, Any]],
) -> str | None:
    """Re-hash every frozen file; the canonical digest is the current build SHA-256.

    Recomputed (not declared) hashes feed the digest, so any byte change in the
    frozen code/material set changes the build identity and invalidates the batch.
    """
    if freeze.get("schema") != FREEZE_SCHEMA:
        findings.append({"code": "FREEZE_MANIFEST_SCHEMA_INVALID"})
        return None
    declared_root = freeze.get("root")
    if not isinstance(declared_root, str) or not declared_root:
        findings.append({"code": "FREEZE_ROOT_MISSING"})
        return None
    try:
        declared_root_path = Path(declared_root).expanduser().resolve(strict=True)
        supplied_root_path = Path(root).expanduser().resolve(strict=True)
    except OSError:
        findings.append({"code": "FREEZE_ROOT_MISSING"})
        return None
    if declared_root_path != supplied_root_path:
        findings.append({"code": "FREEZE_ROOT_BINDING_MISMATCH",
                         "declared": str(declared_root_path),
                         "actual": str(supplied_root_path)})
        return None
    root = declared_root_path
    files = freeze.get("files")
    if not isinstance(files, dict) or not files:
        findings.append({"code": "FREEZE_MANIFEST_INVALID"})
        return None
    if freeze.get("file_count") != len(files):
        findings.append({"code": "FREEZE_FILE_COUNT_MISMATCH",
                         "declared": freeze.get("file_count"),
                         "actual": len(files)})

    declared_paths: set[str] = set()
    lines: list[str] = []
    for rel in sorted(files):
        expected = files[rel]
        if not isinstance(rel, str) or not rel or "\\" in rel:
            findings.append({"code": "FREEZE_MANIFEST_PATH_INVALID", "item": str(rel)})
            continue
        relative = Path(rel)
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != rel:
            findings.append({"code": "FREEZE_MANIFEST_PATH_INVALID", "item": rel})
            continue
        declared_paths.add(rel)
        if not _is_sha256(expected):
            findings.append({"code": "FREEZE_MANIFEST_HASH_INVALID", "item": rel})
            continue
        path = _resolve_rel(root, rel)
        if path is None:
            findings.append({"code": "FROZEN_FILE_MISSING", "item": rel})
            continue
        actual = sha256_file(path)
        if actual != expected:
            findings.append({"code": "FROZEN_FILE_HASH_MISMATCH", "item": rel,
                             "expected": expected, "actual": actual})
        lines.append(f"{actual}  {rel}")

    inventory_findings: list[dict[str, Any]] = []
    actual_inventory = _candidate_build_inventory(root, inventory_findings)
    findings.extend(inventory_findings)
    actual_paths = set(actual_inventory)
    missing = sorted(actual_paths - declared_paths)
    unexpected = sorted(declared_paths - actual_paths)
    if missing:
        findings.append({"code": "FREEZE_INVENTORY_INCOMPLETE", "missing": missing})
    if unexpected:
        findings.append({"code": "FREEZE_INVENTORY_UNEXPECTED_FILES", "paths": unexpected})
    if not lines:
        findings.append({"code": "FREEZE_MANIFEST_EMPTY"})
        return None
    return sha256_bytes("\n".join(lines).encode("utf-8"))


def _excluded_from_freeze(relative: Path) -> bool:
    parts = relative.parts
    if any(part in FREEZE_EXCLUDED_DIRS for part in parts):
        return True
    rel_posix = relative.as_posix()
    if any(rel_posix == prefix or rel_posix.startswith(prefix + "/")
           for prefix in FREEZE_EXCLUDED_PREFIXES):
        return True
    name = relative.name
    if name in FREEZE_EXCLUDED_NAMES or name == ".env" or name.startswith(".env."):
        return True
    return relative.suffix.lower() in FREEZE_EXCLUDED_SUFFIXES


def _candidate_build_inventory(root: Path,
                               findings: list[dict[str, Any]]) -> dict[str, str]:
    """Hash the complete candidate code/material set using fixed exclusions.

    Caller-supplied glob patterns are intentionally unsupported: a narrow glob
    could leave a changed runtime module or image asset outside the build ID.
    """
    try:
        root = root.resolve(strict=True)
    except OSError:
        findings.append({"code": "FREEZE_ROOT_MISSING"})
        return {}
    if not root.is_dir():
        findings.append({"code": "FREEZE_ROOT_MISSING"})
        return {}
    files: dict[str, str] = {}
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for dirname in sorted(dirnames):
            path = current_path / dirname
            relative = path.relative_to(root)
            if _excluded_from_freeze(relative):
                continue
            if path.is_symlink():
                findings.append({"code": "FROZEN_SYMLINK_UNSUPPORTED",
                                 "item": relative.as_posix()})
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for filename in sorted(filenames):
            path = current_path / filename
            relative = path.relative_to(root)
            if _excluded_from_freeze(relative):
                continue
            if path.is_symlink():
                findings.append({"code": "FROZEN_SYMLINK_UNSUPPORTED",
                                 "item": relative.as_posix()})
                continue
            if path.is_file():
                files[relative.as_posix()] = sha256_file(path)
    return files


def build_freeze_manifest(root: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    findings: list[dict[str, Any]] = []
    files = _candidate_build_inventory(root, findings)
    if findings:
        raise ValueError(json.dumps(findings, ensure_ascii=False))
    return {
        "schema": FREEZE_SCHEMA,
        "root": str(root),
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(files),
        "files": files,
    }


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _page_cover(pages: Any, page_count: int) -> set[int]:
    if not isinstance(pages, list):
        return set()
    return {p for p in pages if isinstance(p, int) and not isinstance(p, bool)} & set(
        range(1, page_count + 1)
    )


def _verify_entry(
    entry: dict[str, Any],
    base: Path,
    build_root: Path,
    build_sha: str | None,
    frozen_at: datetime | None,
    seen: dict[str, set[str]],
    findings: list[dict[str, Any]],
) -> bool:
    deck_id = entry.get("deck_id")
    if not isinstance(deck_id, str) or not deck_id:
        findings.append({"code": "DECK_ID_MISSING"})
        return False

    # ---- uniqueness of identity -------------------------------------------------
    for key, value in (
        ("deck_id", deck_id),
        ("task_id", entry.get("task_id")),
        ("source_sha256", entry.get("source_sha256")),
        ("candidate_pptx_sha256", entry.get("candidate_pptx_sha256")),
        ("review_evidence_sha256", (entry.get("review_evidence") or {}).get("sha256")
         if isinstance(entry.get("review_evidence"), dict) else None),
    ):
        if not isinstance(value, str) or not value:
            findings.append({"code": "IDENTITY_FIELD_MISSING", "deck_id": deck_id, "field": key})
            return False
        if value in seen[key]:
            findings.append({"code": "DUPLICATE_IDENTITY", "deck_id": deck_id,
                             "field": key, "value": value})
        seen[key].add(value)

    if not isinstance(entry.get("task_id"), str) or not entry["task_id"]:
        findings.append({"code": "TASK_ID_MISSING", "deck_id": deck_id})
        return False

    # ---- declared flags and verdicts -------------------------------------------
    for flag in FALSE_FLAGS:
        if entry.get(flag) is not False:
            findings.append({"code": "DISQUALIFYING_FLAG_NOT_FALSE", "deck_id": deck_id,
                             "field": flag, "actual": entry.get(flag)})
    for field in PASS_VERDICT_FIELDS:
        if entry.get(field) != "PASS":
            findings.append({"code": "VERDICT_NOT_PASS", "deck_id": deck_id, "field": field,
                             "actual": entry.get(field)})
    languages = entry.get("language_codes")
    if not isinstance(languages, list) or not languages or not all(
        isinstance(code, str) and code for code in languages
    ):
        findings.append({"code": "LANGUAGE_CODES_INVALID", "deck_id": deck_id})

    runtime_environment = entry.get("runtime_environment")
    if not isinstance(runtime_environment, dict):
        findings.append({"code": "RUNTIME_ENVIRONMENT_MISSING", "deck_id": deck_id})
        runtime_environment = {}
    for field in RUNTIME_ENVIRONMENT_FIELDS:
        value = runtime_environment.get(field)
        if field == "dependency_manifest_sha256":
            if not _is_sha256(value):
                findings.append({"code": "RUNTIME_ENVIRONMENT_FIELD_INVALID",
                                 "deck_id": deck_id, "field": field})
        elif not isinstance(value, str) or not value.strip():
            findings.append({"code": "RUNTIME_ENVIRONMENT_FIELD_INVALID",
                             "deck_id": deck_id, "field": field})
    dependency_manifest = build_root / "requirements.txt"
    if not dependency_manifest.is_file():
        findings.append({"code": "DEPENDENCY_MANIFEST_MISSING", "deck_id": deck_id})
    elif runtime_environment.get("dependency_manifest_sha256") != sha256_file(
        dependency_manifest
    ):
        findings.append({"code": "RUNTIME_DEPENDENCY_MANIFEST_MISMATCH", "deck_id": deck_id})

    # ---- input and output identity ---------------------------------------------
    source = _resolve_rel(base, entry.get("source_file"))
    if source is None:
        findings.append({"code": "SOURCE_FILE_MISSING", "deck_id": deck_id})
        return False
    source_sha = sha256_file(source)
    if source_sha != entry.get("source_sha256"):
        findings.append({"code": "SOURCE_IDENTITY_MISMATCH", "deck_id": deck_id,
                         "expected": entry.get("source_sha256"), "actual": source_sha})

    pptx = _resolve_rel(base, entry.get("candidate_pptx"))
    if pptx is None:
        findings.append({"code": "CANDIDATE_PPTX_MISSING", "deck_id": deck_id})
        return False
    pptx_sha = sha256_file(pptx)
    if pptx_sha != entry.get("candidate_pptx_sha256"):
        findings.append({"code": "PPTX_IDENTITY_MISMATCH", "deck_id": deck_id,
                         "expected": entry.get("candidate_pptx_sha256"), "actual": pptx_sha})

    facts = recompute_pptx_facts(pptx)
    if facts is None:
        findings.append({"code": "PPTX_UNREADABLE", "deck_id": deck_id})
        return False
    if facts["page_count"] < 1:
        findings.append({"code": "PPTX_HAS_NO_SLIDES", "deck_id": deck_id})
        return False

    # ---- really inserted scene images (never trust the declared count) ----------
    # Count only picture shapes that resolve to decodable media from a slide's
    # OOXML relationships. Orphaned package media never count as inserted.
    actual_images = facts["slide_pictures"]
    if actual_images < 1:
        findings.append({"code": "NO_SCENE_IMAGE_INSERTED", "deck_id": deck_id,
                         "recomputed": facts})
    claimed = entry.get("actual_scene_images_inserted")
    if not isinstance(claimed, int) or isinstance(claimed, bool) or claimed < 1:
        findings.append({"code": "SCENE_IMAGE_COUNT_INVALID", "deck_id": deck_id,
                         "actual": claimed})
    elif claimed != actual_images:
        findings.append({"code": "SELF_REPORTED_IMAGE_COUNT_MISMATCH", "deck_id": deck_id,
                         "claimed": claimed, "recomputed": actual_images})

    scene_records = entry.get("scene_image_records")
    actual_image_keys = {
        (item["slide"], item["shape_name"], item["sha256"])
        for item in facts["images"]
    }
    declared_image_keys: set[tuple[int, str, str]] = set()
    scene_ids: set[str] = set()
    scene_image_hashes: set[str] = set()
    if not isinstance(scene_records, list) or len(scene_records) != actual_images:
        findings.append({"code": "SCENE_IMAGE_RECORD_COVERAGE_MISMATCH", "deck_id": deck_id,
                         "actual": actual_images,
                         "declared": len(scene_records) if isinstance(scene_records, list) else None})
    else:
        for scene_record in scene_records:
            if not isinstance(scene_record, dict):
                findings.append({"code": "SCENE_IMAGE_RECORD_INVALID", "deck_id": deck_id})
                continue
            slide_no, shape_name, image_sha = (
                scene_record.get("slide"), scene_record.get("shape_name"),
                scene_record.get("image_sha256"),
            )
            key = (slide_no, shape_name, image_sha)
            if key not in actual_image_keys:
                findings.append({"code": "SCENE_IMAGE_NOT_BOUND_TO_PPTX_OBJECT",
                                 "deck_id": deck_id, "slide": slide_no,
                                 "shape_name": shape_name})
            declared_image_keys.add(key)
            scene_id = scene_record.get("canonical_scene_id")
            if not isinstance(scene_id, str) or not scene_id.strip():
                findings.append({"code": "SCENE_ID_MISSING", "deck_id": deck_id})
            elif scene_id in scene_ids:
                findings.append({"code": "DUPLICATE_CANONICAL_SCENE_IMAGE",
                                 "deck_id": deck_id, "canonical_scene_id": scene_id})
            else:
                scene_ids.add(scene_id)
            if isinstance(image_sha, str) and image_sha in scene_image_hashes:
                findings.append({"code": "DUPLICATE_SCENE_IMAGE_BYTES",
                                 "deck_id": deck_id, "image_sha256": image_sha})
            elif isinstance(image_sha, str):
                scene_image_hashes.add(image_sha)
            scene_provider = scene_record.get("image_provider")
            scene_license = scene_record.get("image_license")
            scene_allowed = ALLOWED_IMAGE_PROVIDERS.get(scene_provider)
            if scene_allowed is None or scene_license not in scene_allowed:
                findings.append({"code": "SCENE_IMAGE_LICENSE_NOT_ALLOWED",
                                 "deck_id": deck_id, "provider": scene_provider,
                                 "license": scene_license})
            for field in ("query", "source_url", "image_provider", "image_license",
                          "license_url"):
                if not isinstance(scene_record.get(field), str) or not scene_record[field].strip():
                    findings.append({"code": "SCENE_IMAGE_PROVENANCE_MISSING",
                                     "deck_id": deck_id, "field": field})
    if declared_image_keys != actual_image_keys:
        findings.append({"code": "SCENE_IMAGE_OBJECT_SET_MISMATCH", "deck_id": deck_id})

    # ---- page scope, rendering and review coverage ------------------------------
    scope = entry.get("page_scope") if isinstance(entry.get("page_scope"), dict) else {}
    declared_pages = scope.get("page_count")
    if declared_pages != facts["page_count"]:
        findings.append({"code": "PAGE_COUNT_MISMATCH", "deck_id": deck_id,
                         "declared": declared_pages, "recomputed": facts["page_count"]})
    page_count = facts["page_count"]
    rendered = _page_cover(scope.get("rendered_pages"), page_count)
    reviewed = _page_cover(scope.get("reviewed_pages"), page_count)
    if rendered != set(range(1, page_count + 1)):
        findings.append({"code": "UNRENDERED_PAGES", "deck_id": deck_id,
                         "missing": sorted(set(range(1, page_count + 1)) - rendered)})
    if reviewed != set(range(1, page_count + 1)):
        findings.append({"code": "UNREVIEWED_PAGES", "deck_id": deck_id,
                         "missing": sorted(set(range(1, page_count + 1)) - reviewed)})

    # ---- per-page preview artifacts ---------------------------------------------
    previews = entry.get("preview_artifacts")
    if not isinstance(previews, list) or not previews:
        findings.append({"code": "PREVIEW_ARTIFACTS_MISSING", "deck_id": deck_id})
    else:
        preview_pages: set[int] = set()
        for index, preview in enumerate(previews):
            label = f"{deck_id}.preview[{index}]"
            if not isinstance(preview, dict):
                findings.append({"code": "PREVIEW_ARTIFACT_INVALID", "deck_id": deck_id})
                continue
            page = preview.get("page")
            if isinstance(page, int) and not isinstance(page, bool):
                preview_pages.add(page)
            path = _safe_ref(base, preview, label, findings)
            if path is None:
                findings.append({"code": "PREVIEW_ARTIFACT_MISSING", "deck_id": deck_id,
                                 "page": page})
            elif _validate_preview_png(path) is None:
                findings.append({"code": "PREVIEW_IMAGE_INVALID", "deck_id": deck_id,
                                 "page": page})
        if preview_pages != set(range(1, page_count + 1)):
            findings.append({"code": "PREVIEW_PAGE_COVERAGE_INCOMPLETE", "deck_id": deck_id,
                             "missing": sorted(set(range(1, page_count + 1)) - preview_pages)})

    # ---- download artifact (must be the very same bytes as the candidate) -------
    download = entry.get("download_artifact")
    if not isinstance(download, dict):
        findings.append({"code": "DOWNLOAD_ARTIFACT_MISSING", "deck_id": deck_id})
    else:
        path = _resolve_rel(base, download.get("path"))
        if path is None:
            findings.append({"code": "DOWNLOAD_ARTIFACT_FILE_MISSING", "deck_id": deck_id})
        else:
            downloaded_sha = sha256_file(path)
            if downloaded_sha != pptx_sha:
                findings.append({"code": "DOWNLOAD_ARTIFACT_HASH_MISMATCH", "deck_id": deck_id,
                                 "expected": pptx_sha, "actual": downloaded_sha})
            if download.get("bytes") != path.stat().st_size:
                findings.append({"code": "DOWNLOAD_ARTIFACT_SIZE_MISMATCH", "deck_id": deck_id,
                                 "declared": download.get("bytes"),
                                 "actual": path.stat().st_size})

    # ---- review record bound to this deck and every page ------------------------
    review_path = _safe_ref(base, entry.get("review_evidence"),
                            f"{deck_id}.review_evidence", findings)
    if review_path is None:
        findings.append({"code": "REVIEW_EVIDENCE_MISSING", "deck_id": deck_id})
    else:
        review = _load_json(review_path, f"{deck_id}.review_evidence", findings)
        if review is not None:
            if review.get("deck_id") != deck_id:
                findings.append({"code": "REVIEW_BINDING_MISMATCH", "deck_id": deck_id,
                                 "field": "deck_id"})
            if review.get("candidate_pptx_sha256") != pptx_sha:
                findings.append({"code": "REVIEW_BINDING_MISMATCH", "deck_id": deck_id,
                                 "field": "candidate_pptx_sha256"})
            if _page_cover(review.get("pages"), page_count) != set(range(1, page_count + 1)):
                findings.append({"code": "REVIEW_PAGE_COVERAGE_INCOMPLETE", "deck_id": deck_id})
            if not isinstance(review.get("reviewer_id"), str) or not review["reviewer_id"]:
                findings.append({"code": "REVIEWER_NOT_RECORDED", "deck_id": deck_id})

    # ---- task run record and build freshness ------------------------------------
    record_path = _safe_ref(base, entry.get("run_record"), f"{deck_id}.run_record", findings)
    if record_path is None:
        findings.append({"code": "RUN_RECORD_MISSING", "deck_id": deck_id})
    else:
        record = _load_json(record_path, f"{deck_id}.run_record", findings)
        if record is not None:
            for field, expected in (
                ("task_id", entry.get("task_id")),
                ("deck_id", deck_id),
                ("source_sha256", source_sha),
                ("candidate_pptx_sha256", pptx_sha),
                ("runtime_environment", runtime_environment),
            ):
                if record.get(field) != expected:
                    findings.append({"code": "RUN_RECORD_BINDING_MISMATCH",
                                     "deck_id": deck_id, "field": field})
            if build_sha is None or record.get("build_sha256") != build_sha:
                findings.append({"code": "STALE_RESULT_BUILD_MISMATCH", "deck_id": deck_id,
                                 "declared": record.get("build_sha256"), "current": build_sha})
            if record.get("status") != "COMPLETED":
                findings.append({"code": "RUN_RECORD_NOT_COMPLETED", "deck_id": deck_id,
                                 "status": record.get("status")})

    timestamp = _parse_ts(entry.get("timestamp"))
    if timestamp is None:
        findings.append({"code": "TIMESTAMP_INVALID", "deck_id": deck_id})
    elif frozen_at is not None and timestamp < frozen_at:
        findings.append({"code": "STALE_RESULT_TIMESTAMP", "deck_id": deck_id,
                         "timestamp": entry.get("timestamp"),
                         "frozen_at": frozen_at.isoformat()})

    # ---- provider and licence registration --------------------------------------
    provider = entry.get("provider") if isinstance(entry.get("provider"), dict) else {}
    for field in ("model_provider", "model_id"):
        if not isinstance(provider.get(field), str) or not provider[field].strip():
            findings.append({"code": "MODEL_PROVENANCE_MISSING", "deck_id": deck_id,
                             "field": field})
    image_provider = provider.get("image_provider")
    licence = provider.get("image_license")
    allowed = ALLOWED_IMAGE_PROVIDERS.get(image_provider) if isinstance(
        image_provider, str) else None
    if allowed is None:
        findings.append({"code": "IMAGE_PROVIDER_NOT_ALLOWED", "deck_id": deck_id,
                         "image_provider": image_provider})
    elif licence not in allowed:
        findings.append({"code": "IMAGE_LICENSE_NOT_ALLOWED", "deck_id": deck_id,
                         "image_provider": image_provider, "image_license": licence})
    if not isinstance(provider.get("license_url"), str) or not provider["license_url"].strip():
        findings.append({"code": "IMAGE_LICENSE_URL_MISSING", "deck_id": deck_id})

    return not any(f.get("deck_id") == deck_id for f in findings)


def _audit_deck_manifest_core(
    manifest_path: str | Path,
    freeze_path: str | Path,
    *,
    expected_count: int,
) -> dict[str, Any]:
    """Recompute every deck identity from disk. Fail closed on any gap."""
    findings: list[dict[str, Any]] = []
    manifest_path = Path(manifest_path)
    try:
        manifest_path = manifest_path.resolve(strict=True)
    except OSError:
        return {"schema": SCHEMA, "verdict": "BLOCKED", "expected_count": expected_count,
                "counted_real_decks": 0, "findings": [{"code": "MANIFEST_MISSING"}]}
    base = manifest_path.parent

    freeze_path = Path(freeze_path)
    try:
        freeze_path = freeze_path.resolve(strict=True)
    except OSError:
        return {"schema": SCHEMA, "verdict": "BLOCKED", "expected_count": expected_count,
                "counted_real_decks": 0, "findings": [{"code": "FREEZE_MANIFEST_MISSING"}]}
    freeze = _load_json(freeze_path, "freeze_manifest", findings)
    if freeze is None:
        return {"schema": SCHEMA, "verdict": "BLOCKED", "expected_count": expected_count,
                "counted_real_decks": 0, "findings": findings}

    frozen_at = _parse_ts(freeze.get("frozen_at"))
    if frozen_at is None:
        findings.append({"code": "FREEZE_TIMESTAMP_INVALID"})

    build_root = Path(freeze.get("root", "")) if isinstance(freeze, dict) else Path()
    build_sha = compute_build_identity(build_root, freeze, findings)
    if build_sha is None:
        findings.append({"code": "BUILD_IDENTITY_NOT_COMPUTABLE"})

    manifest = _load_json(manifest_path, "deck_manifest", findings)
    if manifest is None:
        return {"schema": SCHEMA, "verdict": "BLOCKED", "expected_count": expected_count,
                "counted_real_decks": 0, "build_sha256": build_sha, "findings": findings}

    entries = manifest.get("entries")
    if not isinstance(entries, list):
        findings.append({"code": "MANIFEST_ENTRIES_INVALID"})
        entries = []
    if len(entries) != expected_count:
        findings.append({"code": "DECK_COUNT_MISMATCH", "expected": expected_count,
                         "actual": len(entries)})

    seen: dict[str, set[str]] = {
        "deck_id": set(), "task_id": set(), "source_sha256": set(),
        "candidate_pptx_sha256": set(), "review_evidence_sha256": set(),
    }
    verified = 0
    for entry in entries:
        if not isinstance(entry, dict):
            findings.append({"code": "DECK_ENTRY_INVALID"})
            continue
        if _verify_entry(entry, base, build_root, build_sha, frozen_at, seen, findings):
            verified += 1

    counted = min(verified, expected_count)
    if verified != expected_count:
        findings.append({"code": "REAL_DECK_COUNT_BELOW_TARGET", "expected": expected_count,
                         "verified": verified})
    verdict = "PASS" if (verified == expected_count and not findings) else "BLOCKED"
    return {
        "schema": SCHEMA,
        "verdict": verdict,
        "expected_count": expected_count,
        "manifest_entries": len(entries),
        "counted_real_decks": counted,
        "build_sha256": build_sha,
        "frozen_at": freeze.get("frozen_at"),
        "base_contract": "contracts/prepublication_50_decks_gate_v1.json (read-only)",
        "identity_contract": "contracts/prepublication_50_decks_identity_v1.json",
        "findings": findings,
    }


def _verify_release_signatures(
    manifest_path: Path,
    freeze_path: Path,
    trust_store: dict[str, Any],
    findings: list[dict[str, Any]],
) -> None:
    """Verify owner freeze, server-run and qualified reviewer signatures.

    Signatures cover the complete canonical JSON record (excluding only the
    signature envelope). The run record must bind exact output/input/build,
    every preview/download and every actual slide picture. Review signatures
    are bound to the same deck SHA and all pages.
    """
    freeze = _load_json(freeze_path, "freeze_manifest", findings)
    manifest = _load_json(manifest_path, "deck_manifest", findings)
    if freeze is None or manifest is None:
        return
    build_root = Path(freeze.get("root", ""))
    build_sha = compute_build_identity(build_root, freeze, findings)
    _verify_ed25519_document(freeze, trust_store["freeze_signers"],
                             "freeze_manifest", findings)
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        deck_id = entry.get("deck_id")
        record_path = _resolve_rel(manifest_path.parent,
                                   (entry.get("run_record") or {}).get("path")
                                   if isinstance(entry.get("run_record"), dict) else None)
        review_path = _resolve_rel(manifest_path.parent,
                                   (entry.get("review_evidence") or {}).get("path")
                                   if isinstance(entry.get("review_evidence"), dict) else None)
        if record_path is None:
            findings.append({"code": "RUN_RECORD_MISSING", "deck_id": deck_id})
            continue
        record = _load_json(record_path, f"{deck_id}.run_record", findings)
        if record is None:
            continue
        runtime_key_id = _verify_ed25519_document(
            record, trust_store["runtime_signers"], f"{deck_id}.run_record", findings
        )
        expected_bindings = {
            "origin": "workbench_server",
            "event_type": "generation_completed",
            "task_id": entry.get("task_id"),
            "deck_id": deck_id,
            "source_sha256": entry.get("source_sha256"),
            "candidate_pptx_sha256": entry.get("candidate_pptx_sha256"),
            "build_sha256": build_sha,
            "status": "COMPLETED",
            "provider": entry.get("provider"),
            "runtime_environment": entry.get("runtime_environment"),
            "scene_image_records": entry.get("scene_image_records"),
            "preview_artifacts": entry.get("preview_artifacts"),
            "download_sha256": entry.get("candidate_pptx_sha256"),
        }
        for field, expected in expected_bindings.items():
            if record.get(field) != expected:
                findings.append({"code": "SIGNED_RUN_BINDING_MISMATCH",
                                 "deck_id": deck_id, "field": field})
        if runtime_key_id is None:
            findings.append({"code": "TRUSTED_RUNTIME_SIGNATURE_REQUIRED",
                             "deck_id": deck_id})

        if review_path is None:
            findings.append({"code": "REVIEW_EVIDENCE_MISSING", "deck_id": deck_id})
            continue
        review = _load_json(review_path, f"{deck_id}.review_evidence", findings)
        if review is None:
            continue
        reviewer_id = review.get("reviewer_id")
        reviewers = trust_store["reviewers"]
        reviewer = reviewers.get(reviewer_id) if isinstance(reviewer_id, str) else None
        if not isinstance(reviewer, dict) or reviewer.get("status") != "active":
            findings.append({"code": "REVIEWER_NOT_TRUSTED", "deck_id": deck_id,
                             "reviewer_id": reviewer_id})
            continue
        reviewer_key_id = reviewer.get("key_id")
        reviewer_keys = {
            item.get("key_id"): item for item in reviewers.values()
            if isinstance(item, dict) and isinstance(item.get("key_id"), str)
        }
        _verify_ed25519_document(
            review, reviewer_keys, f"{deck_id}.review_evidence",
            findings, expected_key_id=reviewer_key_id,
        )
        page_count = entry.get("page_scope", {}).get("page_count")
        pages = set(range(1, page_count + 1)) if isinstance(page_count, int) else set()
        review_languages = reviewer.get("language_codes")
        deck_languages = entry.get("language_codes")
        scopes = reviewer.get("scopes")
        if review.get("deck_id") != deck_id or review.get("candidate_pptx_sha256") != entry.get(
            "candidate_pptx_sha256"
        ):
            findings.append({"code": "SIGNED_REVIEW_BINDING_MISMATCH", "deck_id": deck_id})
        if _page_cover(review.get("pages"), len(pages)) != pages:
            findings.append({"code": "SIGNED_REVIEW_PAGE_COVERAGE_INCOMPLETE",
                             "deck_id": deck_id})
        if review.get("verdict") != "PASS":
            findings.append({"code": "SIGNED_REVIEW_VERDICT_NOT_PASS", "deck_id": deck_id})
        if review.get("visual_review_verdict") != "PASS" or review.get(
            "content_fidelity_verdict"
        ) != "PASS":
            findings.append({"code": "SIGNED_REVIEW_SCOPE_VERDICTS_MISSING",
                             "deck_id": deck_id})
        if not isinstance(scopes, list) or not {"visual_review", "content_fidelity"}.issubset(
            set(scopes)
        ):
            findings.append({"code": "REVIEWER_SCOPE_INSUFFICIENT", "deck_id": deck_id})
        if not isinstance(review_languages, list) or not isinstance(deck_languages, list) or not set(
            deck_languages
        ).issubset(set(review_languages)):
            findings.append({"code": "REVIEWER_LANGUAGE_QUALIFICATION_MISSING",
                             "deck_id": deck_id})


def audit_deck_manifest(manifest_path: str | Path,
                        freeze_path: str | Path) -> dict[str, Any]:
    """Public release gate. The release target is immutable at exactly 50."""
    trust_findings: list[dict[str, Any]] = []
    trust_store = _load_pinned_trust_store(trust_findings)
    if trust_store is None:
        return {"schema": SCHEMA, "verdict": "BLOCKED",
                "expected_count": REQUIRED_EXPECTED_COUNT, "manifest_entries": 0,
                "counted_real_decks": 0, "findings": trust_findings}
    report = _audit_deck_manifest_core(manifest_path, freeze_path,
                                       expected_count=REQUIRED_EXPECTED_COUNT)
    if report.get("verdict") == "PASS":
        signature_findings: list[dict[str, Any]] = []
        try:
            _verify_release_signatures(Path(manifest_path).resolve(strict=True),
                                       Path(freeze_path).resolve(strict=True),
                                       trust_store, signature_findings)
        except Exception as exc:
            signature_findings.append({"code": "SIGNATURE_VERIFICATION_ERROR",
                                       "error_type": type(exc).__name__})
        if signature_findings:
            report["findings"].extend(signature_findings)
            report["verdict"] = "BLOCKED"
            report["counted_real_decks"] = 0
    return report


def _audit_deck_manifest_for_test(manifest_path: str | Path,
                                  freeze_path: str | Path,
                                  *, expected_count: int = 1) -> dict[str, Any]:
    """Test-only structural helper. Its passing result can never count as release PASS."""
    report = _audit_deck_manifest_core(manifest_path, freeze_path,
                                       expected_count=expected_count)
    report["test_only"] = True
    if report.get("verdict") == "PASS":
        report["verdict"] = "TEST_ONLY_PASS"
        report["test_verified_entries"] = report.get("counted_real_decks", 0)
        report["counted_real_decks"] = 0
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser("verify", help="audit a 50-deck manifest against the freeze")
    verify.add_argument("manifest", type=Path)
    verify.add_argument("--freeze-manifest", type=Path, required=True)
    verify.add_argument("--json-out", type=Path, help="also write the report to this path")

    freeze_cmd = sub.add_parser("freeze", help="emit a freeze manifest from glob patterns")
    freeze_cmd.add_argument("--root", type=Path, required=True)
    freeze_cmd.add_argument("--out", type=Path, required=True)

    args = parser.parse_args(argv)

    if args.command == "freeze":
        try:
            manifest = build_freeze_manifest(args.root)
        except (OSError, ValueError) as exc:
            print(json.dumps({"verdict": "BLOCKED", "error_type": type(exc).__name__,
                              "detail": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 2
        args.out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(json.dumps({"schema": manifest["schema"], "file_count": manifest["file_count"],
                          "frozen_at": manifest["frozen_at"], "out": str(args.out)},
                         ensure_ascii=False, indent=2))
        return 0

    try:
        report = audit_deck_manifest(args.manifest, args.freeze_manifest)
    except Exception as exc:  # fail closed: never let an exception imply a pass
        report = {"schema": SCHEMA, "verdict": "BLOCKED",
                  "expected_count": REQUIRED_EXPECTED_COUNT, "counted_real_decks": 0,
                  "findings": [{"code": "GATE_INTERNAL_ERROR",
                                "detail": f"{type(exc).__name__}: {exc}"}]}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.json_out:
        args.json_out.write_text(text, encoding="utf-8")
    return 0 if report["verdict"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
