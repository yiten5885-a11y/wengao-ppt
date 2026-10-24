# WenGao PPT (稳稿 PPT)

WenGao PPT is a production-grade, **local-first**, and **deterministic contract-driven** presentation automation and visual beautification engine.

> **Core Philosophy**: All quality verdicts must be independently re-verified against physical artifacts on disk (PPTX binaries, exported PDFs, rendered pixel buffers, and font glyph contours). Self-reported status from generators is strictly rejected. Output that fails frozen quality gates is never silently accepted or marked ready for delivery.

---

## Quick Start

### 1. Environment & Installation

```bash
# 1. Install core dependencies (Python 3.10+ recommended)
pip install -r requirements.txt

# 2. Install and activate in an isolated version directory
python -m wengao_ppt.install install --package . --activate

# 3. Launch local workbench (Loopback 127.0.0.1 only)
~/.wengao-ppt/bin/wengao start --port 8823

# 4. Open browser interface
# Navigate to http://127.0.0.1:8823/
```

### 2. Command Line Interface (CLI)

```bash
# Workflow A: Beautify existing presentation
python -m wengao_ppt.cli precheck --input presentation.pptx --mode beautify --theme wengao-basic
python -m wengao_ppt.cli run --job-id <job-id> --timeout 900
python -m wengao_ppt.cli validate --job-id <job-id>
python -m wengao_ppt.cli export --job-id <job-id> --outdir ./output

# Workflow B: End-to-end generation from raw material
python -m wengao_ppt.cli from-material \
  --input business_strategy.md \
  --pages 12 \
  --model qwen3.8:27b-wb65536 \
  --theme wengao-basic \
  --accept
```

---

## Four Core Workflows

| Workflow | Entry Point | Contract Specifications |
| :--- | :--- | :--- |
| **Existing PPTX Beautification** | Studio UI / `precheck` | Strictly preserves user numbers, text, notes, and chart data; applies role-based font floors and collision barriers; infers contextual scene imagery. |
| **Raw Material to Deck** | Material UI / `from-material` | Freezes fact ledger before inference; one local-model structure plan followed by deterministic, source-exact fact assignment; strictly bounded within **10–20 slides**. |
| **Controlled Local Edits** | Edit UI / `edit` | Requires explicit target `(page, shape_id, field, new_value)`; isolates version branches; prevents hallucinated modifications. |
| **Version History & Rollback** | History UI / `edit-restore` | Append-only ledger; integrity hash validation upon rollback; requires complete re-validation before export. |

---

## Hard Quality Gate System (Hard Gates)

Every presentation candidate must pass 100% of the frozen hard gates:

### 1. Layout & Readability Hard Gates (HG01–HG12)
- **HG01 Text Preservation**: Zero text loss, character drops, or unauthorized rewording.
- **HG02 Safe Canvas Placement**: All text containers must reside within slide canvas safe zones.
- **HG03/HG04 Zero Collision & Occlusion**: Text blocks, cards, and shapes must maintain minimum safety buffers with zero visual overlap.
- **HG07 Role-Based Font Size Floors**: Enforces readability (Title: 36–40pt, Section: 28–32pt, Body: 20–24pt, Footnote: >=14pt).
- **HG08 Coherent Semantic Line Breaks**: Natural breaks at semantic punctuation boundaries; no broken multi-character words.
- **HG10 Native Playback Verification**: Requires continuous 30–60s native screen recording followed by disk reopen hash verification.

### 2. Full-Deck Semantic Scene Imagery (HG13)
- **Real Photographic Imagery**: High-relevance, authentic photos across the entire deck; text cards are blocked from replacing images.
- **Actual-pixel visual review**: Every candidate photo must be inspected by the configured local multimodal model for visible subject, action, and setting at high confidence. Search titles and keywords alone never approve an image. The proof binds the source image SHA-256, final embedded bytes, scene identity, and fixed review prompt. An unavailable model, mismatched image, or missing proof blocks generation; text cards are never substituted. The rule is implemented in the candidate code; full real Workbench acceptance remains open.
- **Multilingual Input Hints and Scene Planning**: This release first completes the existing 26-language list: Arabic, Bengali, German, English, Spanish, Persian, French, Hindi, Indonesian, Italian, Japanese, Korean, Malay, Dutch, Polish, Portuguese, Russian, Swahili, Tamil, Telugu, Thai, Turkish, Ukrainian, Urdu, Vietnamese, and Chinese. Greek and Hebrew remain extra script-detection hints; other languages are deferred to a later release. Each page is locally checked for language and script before the scene planner receives its full original text and language context, extracts source-grounded scene keywords, and creates neutral English photo-search queries. Offline recognition and all 26 planner-payload fixtures pass; this does not mean all 26 languages have passed real model semantics, typography, retrieval, and photo-insertion acceptance. Only Chinese and English currently have full end-to-end evidence; other in-scope languages remain partial or pending in the five-layer matrix. This does not claim UI localization.
- **Global Deduplication & Canonical Mapping**: Specific sub-scenes appear at most once per presentation; multilingual synonyms map to canonical IDs.
- **Zero-Crop Contain Safe Area**: Embedded photos must preserve source aspect ratio with zero cropping and verifiable source licenses.

---

## System Requirements & Dependencies

- **OS**: macOS (Apple Silicon Metal acceleration supported), Linux (x86_64/arm64), Windows WSL2.
- **Python**: 3.10 — 3.13.
- **LibreOffice**: 7.6+ / 24.x (Headless real rendering, PDF export, glyph contour verification).
- **llama-server**: b10500+ (llama.cpp Metal/CUDA runtime for GGUF model execution).
- **Fontconfig**: Bundled font configuration ensuring platform-independent typography rendering.

---

## Data Privacy & Zero Model Cost

1. **Strictly Local Processing**: Source decks, extracted facts, financial metrics, and speaker notes remain strictly on-device. Image queries are neutralized to generic search tokens (e.g., `modern factory conveyor`).
2. **Zero SaaS Cost**: Local inference powered by Qwen 27B / 14B GGUF with zero token fees or recurring subscriptions.

### Optional Pexels scene-photo search

- When the Workbench server process is configured with the user's own `PEXELS_API_KEY`, the candidate sends only short, neutral English scene queries to the Pexels API over HTTPS. It does not send slide text, quotations, notes, or business metrics. The key is read from the server process environment and is not written to jobs, slides, or logs.
- Without a key, the Workbench still tries the no-key Commons/Openverse sources. Every result must pass subject/action/setting, license, quality, and deck-wide deduplication checks; a missing relevant image blocks the output instead of being replaced with an unrelated photo or text card.
- Embedded Pexels images retain the photo page, photographer credit, and license source. Do not redistribute raw Pexels photos as a standalone image pack. See the [Pexels API documentation](https://www.pexels.com/api/documentation/) and [Pexels License](https://www.pexels.com/license/). This optional route still needs a configured key and real Workbench end-to-end acceptance.

---

## Troubleshooting Guide

- **`MODEL.RUNTIME_UNAVAILABLE`**: Ensure local `llama-server` is running on `127.0.0.1:18888`.
- **`PAGE_RANGE_UNSATISFIED`**: Source document length produced slides outside [10, 20]. Ensure document has 7–10 well-structured sections.
- **`LAYOUT.HARD_GATE_FAILED`**: Inspect `layout_gate_runs/*/layout_gate.json` for font size, contrast, or missing playback video issues.
- **`SCENE.PHOTO_UNEXPECTED_CROP`**: Ensure placement preserves aspect ratio without edge clipping.

---

## Third-Party Licenses & Image Provenance

- **Upstream Engine**: Based on luobo-ppt, licensed under **MIT** (`vendor/luobo-ppt/LICENSE`).
- **Icons**: Lucide Icons, licensed under **ISC** (`vendor/luobo-ppt/LICENSE-lucide.txt`).
- **Sample Scene Assets**: Development candidates include images under Pexels License. Redistribution of raw photo bundles is prohibited; release builds exclude raw stock photos pending separate rights clearance.
- **Proprietary Code License**: Reserved for user determination before final public release.
