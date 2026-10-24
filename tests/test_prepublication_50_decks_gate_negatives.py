"""Negative cases for the 50-deck pre-publication release gate (WP9).

Every case builds real bytes on disk (a genuinely zipped PPTX, real preview and
download files, real JSON records) and then perturbs exactly one property. Each
test asserts the gate returns ``BLOCKED`` *and* reports the specific finding
code, so a gate that blocked for the wrong reason would still fail the suite.

One mechanism control (``test_complete_evidence_is_not_blocked``) proves the
gate is not trivially rejecting everything. Its deck is a constructed test
artefact and is explicitly NOT counted toward the 50 real decks.
"""
from __future__ import annotations

import copy
import base64
import hashlib
import inspect
import json
import sys
import tempfile
import unittest
import zipfile
from io import BytesIO
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

BUILD_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = BUILD_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from prepublication_50_decks_gate import (  # noqa: E402
    _audit_deck_manifest_for_test,
    _canonical_signed_bytes,
    _validate_preview_png,
    _verify_ed25519_document,
    audit_deck_manifest,
    build_freeze_manifest,
    compute_build_identity,
    recompute_pptx_facts,
    sha256_file,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402
from PIL import Image  # noqa: E402
from pptx import Presentation  # noqa: E402
from pptx.util import Inches  # noqa: E402

PML = "http://schemas.openxmlformats.org/presentationml/2006/main"
PROVIDER = {
    "model_provider": "ollama-local",
    "model_id": "qwen2.5:7b-instruct",
    "image_provider": "wikimedia_commons",
    "image_license": "CC0",
    "image_source_url": "https://commons.wikimedia.org/wiki/File:example.jpg",
    "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_pptx(path: Path, pages: int = 1, pics_per_page: int = 1, media: int = 1) -> None:
    """Write a real OOXML package with correctly related, decodable PNGs."""
    presentation = Presentation()
    blank_layout = presentation.slide_layouts[6]
    image_index = 0
    for _ in range(pages):
        slide = presentation.slides.add_slide(blank_layout)
        for _ in range(pics_per_page):
            color = ((image_index * 37) % 255, (image_index * 71 + 30) % 255,
                     (image_index * 113 + 80) % 255)
            image = Image.new("RGB", (48, 32), color)
            image_bytes = BytesIO()
            image.save(image_bytes, format="PNG")
            slide.shapes.add_picture(BytesIO(image_bytes.getvalue()), Inches(0.5),
                                     Inches(0.5), width=Inches(1), height=Inches(0.67))
            image_index += 1
    presentation.save(path)


def compute_build_sha(root: Path, files: dict[str, str]) -> str:
    lines = [f"{files[rel]}  {rel}" for rel in sorted(files)]
    return sha256_bytes("\n".join(lines).encode("utf-8"))


class DeckFactory:
    """Builds one fully evidenced deck entry on disk."""

    def __init__(self, root: Path, deck_id: str = "deck-001", *, pages: int = 1,
                 pics_per_page: int = 1, media: int = 1, source_body: str | None = None):
        self.root = root
        self.deck_id = deck_id
        self.pages = pages
        self.pics = pics_per_page
        self.media = media
        self.source_body = source_body or f"# {deck_id}\n真实输入材料正文。\n"

    def build(self, *, build_sha: str | None = None, timestamp: str | None = None,
              claimed_images: int | None = None,
              runtime_environment: dict | None = None) -> dict:
        runtime_environment = runtime_environment or {
            "os_name": "Darwin",
            "architecture": "arm64",
            "python_implementation": "CPython",
            "python_version": "3.13.16",
            "dependency_manifest_sha256": "0" * 64,
            "renderer_name": "LibreOffice Impress",
            "renderer_version": "test-fixture",
        }
        source = self.root / f"inputs/{self.deck_id}.md"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(self.source_body, encoding="utf-8")

        pptx = self.root / f"out/{self.deck_id}.pptx"
        pptx.parent.mkdir(parents=True, exist_ok=True)
        write_pptx(pptx, pages=self.pages, pics_per_page=self.pics, media=self.media)
        pptx_sha = sha256_file(pptx)
        pptx_deck = Presentation(str(pptx))
        scene_image_records = []
        for page_number, slide in enumerate(pptx_deck.slides, start=1):
            for shape in slide.shapes:
                if shape.shape_type == 13:
                    scene_image_records.append({
                        "slide": page_number,
                        "shape_name": shape.name,
                        "image_sha256": sha256_bytes(shape.image.blob),
                        "canonical_scene_id": f"{self.deck_id}:scene:{page_number}:{shape.name}",
                        "query": f"synthetic test scene for {self.deck_id}",
                        "source_url": "https://commons.wikimedia.org/wiki/File:example.jpg",
                        "image_provider": "wikimedia_commons",
                        "image_license": "CC0",
                        "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                    })

        download = self.root / f"download/{self.deck_id}.pptx"
        download.parent.mkdir(parents=True, exist_ok=True)
        download.write_bytes(pptx.read_bytes())

        previews = []
        for page in range(1, self.pages + 1):
            preview = self.root / f"previews/{self.deck_id}/p{page}.png"
            preview.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (128, 96), (page * 13 % 255, 32, 64)).save(preview, format="PNG")
            previews.append({"page": page,
                             "path": str(preview.relative_to(self.root)),
                             "sha256": sha256_file(preview)})

        review = self.root / f"reviews/{self.deck_id}.review.json"
        review.parent.mkdir(parents=True, exist_ok=True)
        review.write_text(json.dumps({
            "schema": "prepublication-deck-review/v1",
            "deck_id": self.deck_id,
            "candidate_pptx_sha256": pptx_sha,
            "pages": list(range(1, self.pages + 1)),
            "reviewer_id": "reviewer-alpha",
        }, ensure_ascii=False), encoding="utf-8")

        record = self.root / f"records/{self.deck_id}.run.json"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({
            "schema": "prepublication-deck-run/v1",
            "task_id": f"task-{self.deck_id}",
            "deck_id": self.deck_id,
            "source_sha256": sha256_file(source),
            "candidate_pptx_sha256": pptx_sha,
            "build_sha256": build_sha,
            "runtime_environment": runtime_environment,
            "status": "COMPLETED",
        }, ensure_ascii=False), encoding="utf-8")

        return {
            "deck_id": self.deck_id,
            "task_id": f"task-{self.deck_id}",
            "source_file": str(source.relative_to(self.root)),
            "source_sha256": sha256_file(source),
            "candidate_pptx": str(pptx.relative_to(self.root)),
            "candidate_pptx_sha256": pptx_sha,
            "language_codes": ["zh-CN"],
            "actual_scene_images_inserted": claimed_images if claimed_images is not None
            else self.pics * self.pages,
            "is_synthetic_fixture": False,
            "is_replay": False,
            "is_mock": False,
            "independent_content_fidelity_verdict": "PASS",
            "layout_and_placement_safety_verdict": "PASS",
            "visual_review_verdict": "PASS",
            "candidate_gate_verdict": "PASS",
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
            "code_identity": {"build_sha256": build_sha},
            "runtime_environment": runtime_environment,
            "run_record": {"path": str(record.relative_to(self.root)),
                           "sha256": sha256_file(record)},
            "provider": dict(PROVIDER),
            "scene_image_records": scene_image_records,
            "page_scope": {"page_count": self.pages,
                           "rendered_pages": list(range(1, self.pages + 1)),
                           "reviewed_pages": list(range(1, self.pages + 1))},
            "preview_artifacts": previews,
            "download_artifact": {"path": str(download.relative_to(self.root)),
                                  "sha256": pptx_sha,
                                  "bytes": download.stat().st_size},
            "review_evidence": {"path": str(review.relative_to(self.root)),
                                "sha256": sha256_file(review),
                                "pages": list(range(1, self.pages + 1))},
        }


class GateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp_root = Path(self._tmp.name)
        self.root = tmp_root / "evidence"
        self.root.mkdir()
        self.build_root = tmp_root / "candidate"
        frozen = self.build_root / "code/frozen_module.py"
        frozen.parent.mkdir(parents=True, exist_ok=True)
        frozen.write_text("FROZEN = True\n", encoding="utf-8")
        requirements = self.build_root / "requirements.txt"
        requirements.write_text("python-pptx==1.0.2\n", encoding="utf-8")
        self.frozen_files = {
            "code/frozen_module.py": sha256_file(frozen),
            "requirements.txt": sha256_file(requirements),
        }
        self.build_sha = compute_build_sha(self.root, self.frozen_files)
        self.runtime_environment = {
            "os_name": "Darwin",
            "architecture": "arm64",
            "python_implementation": "CPython",
            "python_version": "3.13.16",
            "dependency_manifest_sha256": sha256_file(requirements),
            "renderer_name": "LibreOffice Impress",
            "renderer_version": "test-fixture",
        }
        self.freeze_document = build_freeze_manifest(self.build_root)
        self.freeze_path = self.root / "freeze.json"
        self.freeze_document["frozen_at"] = datetime.now(timezone.utc).isoformat()
        self.freeze_path.write_text(json.dumps(self.freeze_document, ensure_ascii=False),
                                    encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_gate(self, entries: list[dict], expected_count: int = 1) -> dict:
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"schema": "prepublication-50-decks-manifest/v1",
                                        "entries": entries}, ensure_ascii=False),
                            encoding="utf-8")
        return _audit_deck_manifest_for_test(manifest, self.freeze_path,
                                             expected_count=expected_count)

    def assertBlocked(self, report: dict, code: str) -> None:
        codes = {f.get("code") for f in report["findings"]}
        self.assertEqual(report["verdict"], "BLOCKED")
        self.assertIn(code, codes, f"expected finding {code}, got {sorted(codes)}")

    def one_valid_entry(self, **kwargs) -> dict:
        factory_keys = {"deck_id", "pages", "pics_per_page", "media", "source_body"}
        factory_kwargs = {k: v for k, v in kwargs.items() if k in factory_keys}
        build_kwargs = {k: v for k, v in kwargs.items() if k not in factory_keys}
        build_kwargs.setdefault("build_sha", self.build_sha)
        build_kwargs.setdefault("runtime_environment", self.runtime_environment)
        return DeckFactory(self.root, factory_kwargs.pop("deck_id", "deck-001"),
                           **factory_kwargs).build(**build_kwargs)


class PositiveControl(GateTestCase):
    def test_complete_evidence_is_not_blocked(self) -> None:
        """Mechanism control: the gate can pass, so BLOCKED results are meaningful.

        The deck here is a constructed test artefact; it is never counted as one
        of the 50 real decks.
        """
        report = self.run_gate([self.one_valid_entry()])
        self.assertEqual(report["verdict"], "TEST_ONLY_PASS",
                         json.dumps(report["findings"], ensure_ascii=False))
        self.assertEqual(report["counted_real_decks"], 0)
        self.assertEqual(report["test_verified_entries"], 1)


class ProductionReleaseGateGuards(GateTestCase):
    def test_release_gate_requires_pinned_provisioned_trust_store(self) -> None:
        entries = [{
            "deck_id": f"fake-{index:02d}",
            "task_id": f"fake-task-{index:02d}",
            "is_synthetic_fixture": False,
            "is_replay": False,
            "is_mock": False,
            "candidate_gate_verdict": "PASS",
        } for index in range(50)]
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"schema": "prepublication-50-decks-manifest/v1",
                                        "entries": entries}), encoding="utf-8")
        report = audit_deck_manifest(manifest, self.freeze_path)
        self.assertEqual(report["expected_count"], 50)
        self.assertEqual(report["verdict"], "BLOCKED")
        self.assertEqual(report["counted_real_decks"], 0)
        self.assertIn("TRUST_STORE_UNPROVISIONED",
                      {finding.get("code") for finding in report["findings"]})

    def test_production_api_has_no_expected_count_override(self) -> None:
        self.assertNotIn("expected_count", inspect.signature(audit_deck_manifest).parameters)

    def test_changed_trust_store_is_rejected_by_pinned_hash(self) -> None:
        fake_trust_store = self.root / "untrusted-keys.json"
        fake_trust_store.write_text(json.dumps({
            "schema": "prepublication-50-decks-trust-store/v1",
            "status": "PROVISIONED",
            "freeze_signers": {"attacker": {"status": "active"}},
            "runtime_signers": {"attacker": {"status": "active"}},
            "reviewers": {"attacker": {"status": "active"}},
        }), encoding="utf-8")
        with patch("prepublication_50_decks_gate.TRUST_STORE_PATH", fake_trust_store):
            report = audit_deck_manifest(self.root / "missing.json", self.freeze_path)
        self.assertEqual(report["verdict"], "BLOCKED")
        self.assertIn("TRUST_STORE_HASH_MISMATCH",
                      {finding.get("code") for finding in report["findings"]})


class ArtifactAndAttestationValidation(GateTestCase):
    def test_invalid_fake_ooxml_zip_is_not_a_deck(self) -> None:
        fake = self.root / "fake.pptx"
        write_pptx(fake)
        # This helper writes a valid package; replacing it with a zip containing
        # only a slide-shaped XML part must fail presentation/relation parsing.
        with zipfile.ZipFile(fake, "w") as package:
            package.writestr("ppt/slides/slide1.xml", "<p:sld xmlns:p='urn:p' />")
        self.assertIsNone(recompute_pptx_facts(fake))

    def test_preview_must_be_decodable_png(self) -> None:
        invalid = self.root / "preview.png"
        invalid.write_bytes(b"\\x89PNG\\r\\n\\x1a\\nFAKE")
        self.assertIsNone(_validate_preview_png(invalid))

    def test_ed25519_signature_detects_tampering(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        public_raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        signed = {"schema": "test/v1", "task_id": "trusted-task", "status": "COMPLETED"}
        signed["signature"] = {
            "key_id": "runtime-test",
            "signature_base64": base64.b64encode(
                private_key.sign(_canonical_signed_bytes(signed))
            ).decode("ascii"),
        }
        registry = {"runtime-test": {
            "status": "active",
            "public_key_base64": base64.b64encode(public_raw).decode("ascii"),
        }}
        findings: list[dict] = []
        self.assertEqual(_verify_ed25519_document(signed, registry, "signed", findings),
                         "runtime-test")
        self.assertEqual(findings, [])
        signed["status"] = "FAILED"
        self.assertIsNone(_verify_ed25519_document(signed, registry, "tampered", findings))
        self.assertIn("SIGNED_ATTESTATION_INVALID", {item["code"] for item in findings})

    def test_different_subscenes_require_distinct_images_and_pass_test_only(self) -> None:
        entry = self.one_valid_entry(pics_per_page=2, media=2)
        report = self.run_gate([entry])
        self.assertEqual(report["verdict"], "TEST_ONLY_PASS")
        self.assertEqual(report["counted_real_decks"], 0)

    def test_duplicate_canonical_scene_is_blocked(self) -> None:
        entry = self.one_valid_entry(pics_per_page=2, media=2)
        entry["scene_image_records"][1]["canonical_scene_id"] = (
            entry["scene_image_records"][0]["canonical_scene_id"]
        )
        self.assertBlocked(self.run_gate([entry]), "DUPLICATE_CANONICAL_SCENE_IMAGE")

    def test_duplicate_image_bytes_for_distinct_scenes_are_blocked(self) -> None:
        entry = self.one_valid_entry(pics_per_page=2, media=2)
        entry["scene_image_records"][1]["image_sha256"] = (
            entry["scene_image_records"][0]["image_sha256"]
        )
        self.assertBlocked(self.run_gate([entry]), "DUPLICATE_SCENE_IMAGE_BYTES")


class NegativeCases(GateTestCase):
    def test_missing_required_field_blocks(self) -> None:
        entry = self.one_valid_entry()
        del entry["download_artifact"]
        self.assertBlocked(self.run_gate([entry]), "DOWNLOAD_ARTIFACT_MISSING")

    def test_missing_review_evidence_blocks(self) -> None:
        entry = self.one_valid_entry()
        del entry["review_evidence"]
        self.assertBlocked(self.run_gate([entry]), "IDENTITY_FIELD_MISSING")

    def test_wrong_source_hash_blocks(self) -> None:
        entry = self.one_valid_entry()
        entry["source_sha256"] = "0" * 64
        self.assertBlocked(self.run_gate([entry]), "SOURCE_IDENTITY_MISMATCH")

    def test_wrong_pptx_hash_blocks(self) -> None:
        entry = self.one_valid_entry()
        entry["candidate_pptx_sha256"] = "1" * 64
        self.assertBlocked(self.run_gate([entry]), "PPTX_IDENTITY_MISMATCH")

    def test_duplicate_identity_blocks(self) -> None:
        first = self.one_valid_entry()
        second = DeckFactory(self.root, "deck-002").build(build_sha=self.build_sha)
        second["candidate_pptx_sha256"] = first["candidate_pptx_sha256"]
        self.assertBlocked(self.run_gate([first, second], expected_count=2),
                           "DUPLICATE_IDENTITY")

    def test_duplicate_task_id_blocks(self) -> None:
        first = self.one_valid_entry()
        second = DeckFactory(self.root, "deck-002").build(build_sha=self.build_sha)
        second["task_id"] = first["task_id"]
        self.assertBlocked(self.run_gate([first, second], expected_count=2),
                           "DUPLICATE_IDENTITY")

    def test_stale_result_build_mismatch_blocks(self) -> None:
        entry = self.one_valid_entry()
        entry["code_identity"]["build_sha256"] = "2" * 64
        record = self.root / "records/deck-001.run.json"
        payload = json.loads(record.read_text(encoding="utf-8"))
        payload["build_sha256"] = "2" * 64
        record.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        entry["run_record"]["sha256"] = sha256_file(record)
        self.assertBlocked(self.run_gate([entry]), "STALE_RESULT_BUILD_MISMATCH")

    def test_stale_timestamp_before_freeze_blocks(self) -> None:
        entry = self.one_valid_entry(
            timestamp=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat())
        self.assertBlocked(self.run_gate([entry]), "STALE_RESULT_TIMESTAMP")

    def test_forged_pass_without_artifacts_blocks(self) -> None:
        entry = self.one_valid_entry()
        (self.root / "out/deck-001.pptx").unlink()
        (self.root / "download/deck-001.pptx").unlink()
        self.assertBlocked(self.run_gate([entry]), "CANDIDATE_PPTX_MISSING")

    def test_forged_pass_verdict_only_blocks(self) -> None:
        entry = self.one_valid_entry()
        entry["visual_review_verdict"] = "PASS"
        entry["candidate_gate_verdict"] = "REVIEWED_AND_VERIFIED"
        self.assertBlocked(self.run_gate([entry]), "VERDICT_NOT_PASS")

    def test_no_scene_image_blocks(self) -> None:
        entry = self.one_valid_entry(pics_per_page=0, media=0, claimed_images=3)
        self.assertBlocked(self.run_gate([entry]), "NO_SCENE_IMAGE_INSERTED")

    def test_self_reported_image_count_mismatch_blocks(self) -> None:
        entry = self.one_valid_entry(pics_per_page=1, media=1, claimed_images=3)
        self.assertBlocked(self.run_gate([entry]), "SELF_REPORTED_IMAGE_COUNT_MISMATCH")

    def test_missing_preview_blocks(self) -> None:
        entry = self.one_valid_entry(pages=2, pics_per_page=1, media=1)
        (self.root / "previews/deck-001/p2.png").unlink()
        report = self.run_gate([entry])
        self.assertBlocked(report, "PREVIEW_ARTIFACT_MISSING")
        self.assertBlocked(report, "EVIDENCE_FILE_MISSING")

    def test_preview_page_coverage_incomplete_blocks(self) -> None:
        entry = self.one_valid_entry(pages=2, pics_per_page=1, media=1)
        entry["preview_artifacts"] = [entry["preview_artifacts"][0]]
        self.assertBlocked(self.run_gate([entry]), "PREVIEW_PAGE_COVERAGE_INCOMPLETE")

    def test_missing_download_blocks(self) -> None:
        entry = self.one_valid_entry()
        (self.root / "download/deck-001.pptx").unlink()
        self.assertBlocked(self.run_gate([entry]), "DOWNLOAD_ARTIFACT_FILE_MISSING")

    def test_download_failure_hash_mismatch_blocks(self) -> None:
        entry = self.one_valid_entry()
        broken = self.root / "download/deck-001.pptx"
        broken.write_bytes(b"CORRUPTED-DOWNLOAD")
        self.assertBlocked(self.run_gate([entry]), "DOWNLOAD_ARTIFACT_HASH_MISMATCH")

    def test_unrendered_pages_blocks(self) -> None:
        entry = self.one_valid_entry(pages=3, pics_per_page=1, media=1)
        entry["page_scope"]["rendered_pages"] = [1, 2]
        self.assertBlocked(self.run_gate([entry]), "UNRENDERED_PAGES")

    def test_synthetic_fixture_flag_blocks(self) -> None:
        entry = self.one_valid_entry()
        entry["is_synthetic_fixture"] = True
        self.assertBlocked(self.run_gate([entry]), "DISQUALIFYING_FLAG_NOT_FALSE")

    def test_replay_flag_blocks(self) -> None:
        entry = self.one_valid_entry()
        entry["is_replay"] = True
        self.assertBlocked(self.run_gate([entry]), "DISQUALIFYING_FLAG_NOT_FALSE")

    def test_mock_flag_blocks(self) -> None:
        entry = self.one_valid_entry()
        entry["is_mock"] = True
        self.assertBlocked(self.run_gate([entry]), "DISQUALIFYING_FLAG_NOT_FALSE")

    def test_forged_review_record_hash_mismatch_blocks(self) -> None:
        entry = self.one_valid_entry()
        review = self.root / "reviews/deck-001.review.json"
        review.write_text(review.read_text(encoding="utf-8") + "\n/* tampered */",
                          encoding="utf-8")
        self.assertBlocked(self.run_gate([entry]), "EVIDENCE_HASH_MISMATCH")

    def test_forged_review_record_wrong_binding_blocks(self) -> None:
        entry = self.one_valid_entry()
        review = self.root / "reviews/deck-001.review.json"
        payload = json.loads(review.read_text(encoding="utf-8"))
        payload["candidate_pptx_sha256"] = "3" * 64
        review.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        entry["review_evidence"]["sha256"] = sha256_file(review)
        self.assertBlocked(self.run_gate([entry]), "REVIEW_BINDING_MISMATCH")

    def test_review_page_coverage_incomplete_blocks(self) -> None:
        entry = self.one_valid_entry(pages=2, pics_per_page=1, media=1)
        review = self.root / "reviews/deck-001.review.json"
        payload = json.loads(review.read_text(encoding="utf-8"))
        payload["pages"] = [1]
        review.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        entry["review_evidence"]["sha256"] = sha256_file(review)
        self.assertBlocked(self.run_gate([entry]), "REVIEW_PAGE_COVERAGE_INCOMPLETE")

    def test_run_record_not_completed_blocks(self) -> None:
        entry = self.one_valid_entry()
        record = self.root / "records/deck-001.run.json"
        payload = json.loads(record.read_text(encoding="utf-8"))
        payload["status"] = "RUNNING"
        record.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        entry["run_record"]["sha256"] = sha256_file(record)
        self.assertBlocked(self.run_gate([entry]), "RUN_RECORD_NOT_COMPLETED")

    def test_runtime_environment_is_required(self) -> None:
        entry = self.one_valid_entry()
        del entry["runtime_environment"]
        self.assertBlocked(self.run_gate([entry]), "RUNTIME_ENVIRONMENT_MISSING")

    def test_runtime_record_must_bind_the_declared_environment(self) -> None:
        entry = self.one_valid_entry()
        record = self.root / "records/deck-001.run.json"
        payload = json.loads(record.read_text(encoding="utf-8"))
        payload["runtime_environment"]["renderer_version"] = "different-renderer"
        record.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        entry["run_record"]["sha256"] = sha256_file(record)
        self.assertBlocked(self.run_gate([entry]), "RUN_RECORD_BINDING_MISMATCH")

    def test_runtime_dependency_manifest_hash_is_recomputed(self) -> None:
        entry = self.one_valid_entry()
        entry["runtime_environment"]["dependency_manifest_sha256"] = "0" * 64
        record = self.root / "records/deck-001.run.json"
        payload = json.loads(record.read_text(encoding="utf-8"))
        payload["runtime_environment"] = copy.deepcopy(entry["runtime_environment"])
        record.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        entry["run_record"]["sha256"] = sha256_file(record)
        self.assertBlocked(self.run_gate([entry]), "RUNTIME_DEPENDENCY_MANIFEST_MISMATCH")

    def test_deck_count_below_50_blocks(self) -> None:
        entry = self.one_valid_entry()
        report = self.run_gate([entry], expected_count=50)
        self.assertBlocked(report, "DECK_COUNT_MISMATCH")
        self.assertBlocked(report, "REAL_DECK_COUNT_BELOW_TARGET")

    def test_frozen_code_change_invalidates_batch(self) -> None:
        """Any byte change in the frozen set invalidates every result of the batch."""
        entry = self.one_valid_entry()
        frozen = self.build_root / "code/frozen_module.py"
        frozen.write_text("FROZEN = False\n", encoding="utf-8")
        report = self.run_gate([entry])
        self.assertBlocked(report, "FROZEN_FILE_HASH_MISMATCH")
        self.assertBlocked(report, "STALE_RESULT_BUILD_MISMATCH")

    def test_missing_freeze_manifest_blocks(self) -> None:
        entry = self.one_valid_entry()
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"entries": [entry]}, ensure_ascii=False),
                            encoding="utf-8")
        report = _audit_deck_manifest_for_test(manifest, self.root / "nope.json",
                                               expected_count=1)
        self.assertBlocked(report, "FREEZE_MANIFEST_MISSING")

    def test_escape_path_rejected(self) -> None:
        entry = self.one_valid_entry()
        entry["source_file"] = "../outside.md"
        self.assertBlocked(self.run_gate([entry]), "SOURCE_FILE_MISSING")

    def test_image_provider_not_allowed_blocks(self) -> None:
        entry = self.one_valid_entry()
        entry["provider"]["image_provider"] = "unknown-stock"
        self.assertBlocked(self.run_gate([entry]), "IMAGE_PROVIDER_NOT_ALLOWED")

    def test_image_license_url_missing_blocks(self) -> None:
        entry = self.one_valid_entry()
        entry["provider"]["license_url"] = ""
        self.assertBlocked(self.run_gate([entry]), "IMAGE_LICENSE_URL_MISSING")


class BuildIdentityTests(GateTestCase):
    def test_build_identity_recomputed_from_disk(self) -> None:
        findings: list[dict] = []
        sha = compute_build_identity(self.build_root, self.freeze_document, findings)
        self.assertEqual(sha, self.build_sha)
        self.assertEqual(findings, [])

    def test_build_identity_detects_missing_frozen_file(self) -> None:
        findings: list[dict] = []
        freeze = copy.deepcopy(self.freeze_document)
        freeze["files"]["code/ghost.py"] = "4" * 64
        freeze["file_count"] = len(freeze["files"])
        compute_build_identity(self.build_root, freeze, findings)
        self.assertIn("FROZEN_FILE_MISSING", {f["code"] for f in findings})

    def test_new_candidate_file_after_freeze_invalidates_inventory(self) -> None:
        (self.build_root / "assets").mkdir()
        (self.build_root / "assets/new-scene.png").write_bytes(b"new candidate material")
        findings: list[dict] = []
        compute_build_identity(self.build_root, self.freeze_document, findings)
        self.assertIn("FREEZE_INVENTORY_INCOMPLETE", {f["code"] for f in findings})

    def test_manifest_evidence_root_may_be_separate_from_candidate_root(self) -> None:
        entry = self.one_valid_entry()
        report = self.run_gate([entry])
        self.assertEqual(report["verdict"], "TEST_ONLY_PASS",
                         json.dumps(report["findings"], ensure_ascii=False))
        self.assertEqual(report["build_sha256"], self.build_sha)

    def test_build_identity_rejects_wrong_candidate_root(self) -> None:
        other_root = self.root / "other-candidate"
        other_root.mkdir()
        findings: list[dict] = []
        self.assertIsNone(compute_build_identity(other_root, self.freeze_document, findings))
        self.assertIn("FREEZE_ROOT_BINDING_MISMATCH", {f["code"] for f in findings})

    def test_duplicate_entries_are_detected_by_copy(self) -> None:
        entry = self.one_valid_entry()
        clone = copy.deepcopy(entry)
        self.assertBlocked(self.run_gate([entry, clone], expected_count=2),
                           "DUPLICATE_IDENTITY")


if __name__ == "__main__":
    unittest.main(verbosity=2)
