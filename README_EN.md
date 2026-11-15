# WenGao PPT (稳稿 PPT)

WenGao PPT is a high-quality PPTX generation and beautification engine built on **local-first** and **deterministic contract** principles.

> **Core philosophy**: every verdict must be produced by an independent verifier that physically re-reads what actually landed on disk (PPTX / PDF / rendered pixels / glyph outlines). The generator is **never trusted to self-report**. Artifacts that fail the Hard Gates are never silently released or auto-marked ready.

---

## 0. Read this first: status and trust boundaries

| Item | Value | Evidence |
| :--- | :--- | :--- |
| Delivery status | `DEVELOPMENT_CANDIDATE`, `release_ready=false` | No entry point may issue READY on its own |
| Own-code version | `0.1.0` | `__version__` in `wengao_ppt/__init__.py`; verified by `python -m wengao_ppt.cli --version` → `0.1.0` |
| Published? | **No.** Not on GitHub or SkillHub | Pre-release checklist: see the last section of `SKILL.md` |
| License (own code) | **MIT** (owner decision, 2026-11-04); `LICENSE` is present at repo root | See `LICENSE-STATUS.md` §3 and `LICENSE` |

**Trust levels used in this document** (do not upgrade them):

| Mark | Meaning |
| :--- | :--- |
| `[VERIFIED]` | Actually executed on this candidate machine; output captured in the P2 delivery report |
| `[DRAFT-UNVERIFIED]` | Not yet validated by a clean install; derived from code and design. Do **not** present it as a verified step |

Real end-to-end capability, stated honestly:

- **26-language real workbench chain: 0/26.** No usable local model and no LibreOffice on this machine; the historical real job `job-167a1306f549` ended `FAILED` / `semantic_scenes` / `VISUAL.SCENE_UNRESOLVED`. Offline language detection and 26 planner-input samples pass as unit data — that is **not** proof the real chain works.
- **50 real decks: 0/50.**
- Known open items are listed in section 10 (Capabilities and limitations).

---

## 1. Quick start

> Except where marked `[VERIFIED]`, the commands below are `[DRAFT-UNVERIFIED]`: **no clean-install verification has been performed yet**. Validate them in your own environment before publishing them anywhere.
> Port convention: pick any free loopback port. **Do not use 8821** — that belongs to a separate production entry point unrelated to this project and must not be switched or overwritten.

```bash
# 1. Install Python dependencies (Python 3.10+ recommended) [DRAFT-UNVERIFIED]
pip install -r requirements.txt

# 2. Install and activate a controlled version (program and user data stay separate) [DRAFT-UNVERIFIED]
python -m wengao_ppt.install install --package . --activate

# 3. Start the local workbench (loopback only, never exposed) [DRAFT-UNVERIFIED]
<install-root>/bin/wengao start --port 8823

# 4. Open http://127.0.0.1:8823/ in your browser
```

Entry-point sanity check (**verified** on this candidate using its own venv, Python 3.13):

```bash
cd <candidate-dir>
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m wengao_ppt.cli --help     # exit 0
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m wengao_ppt.cli --version  # 0.1.0
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m wengao_ppt.install --help # exit 0
```

### 1.1 CLI usage

There is exactly one entry point. Subcommands actually present in `--help` output (**verified**): **from-material / edit / edit-list / edit-restore / precheck / run / cancel / submit / publish / validate / export / status (job-status) / incident-record / incident-list / cleanup**.

```bash
# A. Beautify an existing PPTX [DRAFT-UNVERIFIED]
python -m wengao_ppt.cli precheck --input presentation.pptx --mode beautify --theme wengao-basic
python -m wengao_ppt.cli run --job-id <job-id> --timeout 900
python -m wengao_ppt.cli validate --job-id <job-id>
python -m wengao_ppt.cli export --job-id <job-id> --outdir ./output

# B. Generate a deck from raw material [DRAFT-UNVERIFIED]
python -m wengao_ppt.cli from-material \
  --input strategy_report.md \
  --pages 12 \
  --model qwen3.8:27b-wb65536 \
  --theme wengao-basic \
  --accept
```

---

## 2. System requirements and dependencies

| Component | Requirement | Purpose |
| :--- | :--- | :--- |
| **OS** | macOS (Apple Silicon recommended) / Linux / Windows WSL2 | macOS Metal acceleration for local models |
| **Python** | 3.10 — 3.13 | Runtime (candidate venv here is 3.13) |
| **LibreOffice** | 7.6+ / 24.x | Headless rendering, PDF export, glyph-outline verification |
| **Local inference runtime** | llama-server (llama.cpp) b10500+ or Ollama | Serves local GGUF weights over loopback HTTP |
| **Fontconfig** | 2.13+ | Cross-platform font resolution. `assets/fontconfig/fonts.conf` ships with the package; **no font files are bundled** |

Python dependencies (`requirements.txt`, generated from real imports): `Pillow`, `lxml`, `openpyxl`, `pypdf`, `pypdfium2`, `python-docx`, `python-pptx`.

> **Version pinning caveat**: the "verified versions" commented in `requirements.txt` are what one acceptance round actually used (macOS / Python 3.13.12 / 2026-10-20). Real installs differ (this candidate's own venv measures Pillow 13.0.0 and pypdf 6.21.2). All three sources of truth — requirements comment / local install / PyPI latest — are listed side by side in `LICENSE-MATRIX.md` §2. Verify in your target environment; do not assume cross-platform parity.

---

## 3. Install / start / upgrade / rollback

Subcommands exposed by the installer (`python -m wengao_ppt.install --help`, **verified**):
`install / upgrade / rollback / status / versions / start / launcher / cli / uninstall`.

```bash
# Install a package directory [DRAFT-UNVERIFIED]
python -m wengao_ppt.install install --package <pkg-dir> [--label L] [--activate]

# Upgrade (self-test must pass before the pointer switches) [DRAFT-UNVERIFIED]
python -m wengao_ppt.install upgrade --package <new-pkg-dir> [--label L]

# Roll back [DRAFT-UNVERIFIED]
python -m wengao_ppt.install rollback [--to L]

# Uninstall: dry-run by default; --apply only moves the program directory into quarantine/, data stays [DRAFT-UNVERIFIED]
python -m wengao_ppt.install uninstall [--apply]
```

Layout (program and data are separated):

- Install root defaults to `~/.wengao-ppt` (override via `--root` or `WENGAO_HOME`);
- Programs live in `versions/<label>/`, config and jobs in `data/` (override via `WENGAO_DATA_ROOT`);
- Launcher port is overridable with `WENGAO_PORT` (default 8791);
- On macOS you can double-click `<install-root>/启动稳稿PPT.command` (if the port is taken it prints the address instead of starting a second instance).

**Install hygiene (implemented and covered by dedicated tests)**: copying excludes `jobs/`, `reports/`, `material_tasks/`, `wb_loopback_data/`, `workbench_previews/`, `workbench_uploads/`, `.wengao_scene_assets/`, `.venv/`, `.git/`, `__pycache__/`, logs and temp files. If any of these still appear after copying, the install **fails closed** rather than leaving a half-dirty install.

---

## 4. Model configuration / cost / data flow

### 4.1 Configuration

| Env var | Default / behaviour | Purpose |
| :--- | :--- | :--- |
| `WENGAO_LOCAL_MODEL_ENDPOINT` | Probes `http://127.0.0.1:18888`, then `http://127.0.0.1:11434`; falls back to `http://127.0.0.1:18888` | Local inference endpoint |
| `WENGAO_FALLBACK_MODEL_ENDPOINT` | `http://127.0.0.1:18888` | Fallback local endpoint |
| `WENGAO_HOME` | `~/.wengao-ppt` | Install root |
| `WENGAO_DATA_ROOT` | falls back to the package dir | Data root (`jobs/` `reports/`) |
| `WENGAO_ASSETS_ROOT` | empty | Explicit controlled asset root for pip layouts |
| `WENGAO_PORT` | `8791` | Launcher port |
| `PEXELS_API_KEY` | empty | Optional Pexels retrieval (see 4.3) |

Model calls target **loopback** endpoints only. The package ships **no model weights** and **no font files** (measured font-file count across the tree = **0**; see `LICENSE-MATRIX.md` §3.3).

### 4.2 Cost

- Local models (llama.cpp / Ollama serving local GGUF) incur **no API billing**; the cost is local compute and memory.
- If you point it at any hosted model or keyed image API, **you pay for it**; this project never purchases credits or pays on your behalf.
- Any "zero cost" claim holds only for the local inference path.

### 4.3 What leaves your machine

| Scenario | Leaves localhost? | Detail |
| :--- | :--- | :--- |
| Local model inference | No | Text goes to a process on `127.0.0.1` |
| Pexels retrieval (**optional**, requires `PEXELS_API_KEY`) | **Yes**, HTTPS to `https://api.pexels.com` | Only **short neutral English scene queries** (e.g. `supermarket checkout`). No source sentences, quotes, notes or business figures. The key is read from the workbench process env only and is **not written to job records, decks or logs** |
| Commons / Openverse retrieval (keyless path) | **Yes**, HTTPS to public APIs | Same: neutral queries only |
| Telemetry | No | Off by default; debug bundles are not auto-uploaded |

> The Pexels / Commons / Openverse paths still need a real workbench end-to-end run before they can be called usable; today's local result is a failure (see §0).

---

## 5. Synthetic example (no real corporate data required)

`samples/material_replay/` ships a synthetic pack:

| File | sha256 (first 16) | Bytes | Note |
| :--- | :--- | ---: | :--- |
| `annual_review_synthetic.md` | `659255db46d1aba1…` | 2882 | Synthetic business material (fictional org and figures, labelled as synthetic at the top of the file) |
| `annual_review_17p.plan.json` | `9f68e6809144447a…` | 18510 | Frozen 17-page plan replay; does not call a model |
| `annual_review_17p_deduplicated.plan.json` | `b1053544284d62e6…` | 16576 | Same, with duplicate facts removed |
| `index.json` | `4ce15ec92335b25f…` | 955 | Pack index and the "this is a replay, not a fresh model call" note |

Why this exists: the replay pack exercises only the **build** stage — it proves whether frozen facts land in the PPTX as planned, on a machine with neither a local model nor LibreOffice. It proves **nothing** about model planning, real rendering, or the gates.

> `samples/open-source-50-decks/multilingual-inputs/` additionally holds 50 multilingual synthetic inputs (each declares itself fictional), used for multilingual acceptance; their batch records sit in `generation-evidence/`. Those recorded verdicts belong to the owning work package — **do not treat intermediate results as final acceptance**.

---

## 6. Four business flows

| Flow | Entry | Rules and gates |
| :--- | :--- | :--- |
| **Beautify existing PPTX** | Workbench "presentation studio" / `precheck` | Preserve every original fact, sentence, note and chart datum; enforce role-level minimum font sizes and no-occlusion layout; full-deck semantic scene image planning |
| **Generate from raw material** | Workbench "material generation" / `from-material` | Extract and freeze the must-say fact ledger; the local model decides global structure, the program injects frozen sentences per section (no per-section serial calls); page count must fall in the **10—20** contract |
| **Controlled local edit** | Workbench "local edit" / `edit` | Must specify exactly `(page, shape id, field, new value)`; each change branches its own version; the model may never freely rewrite pages it was not authorized to touch |
| **Versioning and restore** | Workbench "history & restore" / `edit-restore` | History is append-only; restoring requires integrity comparison; the restored deck must re-pass full acceptance |

---

## 7. Hard gates

A candidate deck must pass **all** of the following independent gates before release. No exceptions.

### 7.1 Layout and readability (HG01—HG12)

- **HG01 Text completeness / zero loss**: every character of the source must reappear in the final PPTX — no lost characters, digits or key terms.
- **HG02 Visible and in-bounds**: every text frame sits inside the safe area, never overflowing the slide.
- **HG03/HG04 Zero collision / zero occlusion**: text blocks, cards, charts and decorative shapes keep safe spacing; text is never covered.
- **HG07 Role-level minimum font size**: title 36–40pt, main heading 28–32pt, body 20–24pt, footnote ≥ 14pt.
- **HG08 Semantic line breaking**: long Chinese sentences break at punctuation or sensible boundaries, never mid-word.
- **HG10 Native playback and reopen**: 30–60 s continuous native playback recording; after the show, close and reopen the file from disk and verify SHA-256.

### 7.2 Deck-wide semantic scene imagery (HG13)

- **Real photography deck-wide**: imagery is planned per scene; stocky clip-art templates and meaningless colour blocks are rejected.
- **Pixel-level visual verification**: each candidate photo must be inspected by the configured **local** multimodal model, confirming subject, action and environment at high confidence. Title or keyword matching alone never passes. Evidence binds source SHA-256, the embedded image, scene semantics and the fixed review prompt. Unavailable model, unsatisfied match or missing evidence blocks generation — never substituted by a text card. The rule is wired into the candidate code; **full real workbench acceptance is still open**.
- **Multilingual hints and scene planning**: this version targets 26 languages (Arabic, Bengali, German, English, Spanish, Persian, French, Hindi, Indonesian, Italian, Japanese, Korean, Malay, Dutch, Polish, Portuguese, Russian, Swahili, Tamil, Telugu, Thai, Turkish, Ukrainian, Urdu, Vietnamese, Chinese). Greek and Hebrew only contribute extra script hints and are outside this 26-language acceptance list; other languages are deferred. Each page detects language and script locally, then passes the unabridged source plus that context to the scene planner, which derives keyword scenes from the source language and emits neutral English search terms. Offline detection and 26 planner-input samples pass unit tests; **that is not proof that the 26 languages completed real model semantics, typesetting, image retrieval and insertion acceptance**. Only Chinese and English have full end-to-end evidence; everything else is marked partially supported or unverified. This does not imply UI localization.
- **Deck-wide dedup and canonical scene ids**: the same concrete scene appears at most once per deck; cross-lingual synonyms map to one canonical id and dedupe automatically.
- **Licensing and zero-crop contract**: every embedded image carries source, license, creator and unique SHA-256; the original aspect ratio is preserved with **zero-crop contain** embedding.

---

## 8. Compatibility

| Dimension | Support |
| :--- | :--- |
| OS | macOS (exercised on Apple Silicon) / Linux / Windows WSL2 — **Linux and Windows not exercised here** |
| Python | 3.10 — 3.13 (verified 3.13 here) |
| Inputs (from material) | `.md / .markdown / .txt / .text / .docx`; `.pptx` is explicitly rejected |
| Inputs (beautify) | `.pptx` |
| Output | `.pptx`; PDF/PNG export optional (requires LibreOffice) |
| Themes | `wengao-basic` / `wengao-business` / `wengao-dark` / `wengao-minimal` / `wengao-adaptive` |
| Slide format | Material flow is fixed 16:9 with a 10—20 page contract |
| Languages | 26-language target list (§7.2); **only Chinese and English have full end-to-end evidence** |
| Explicitly unsupported | Editing chart parts shared across pages (rejected outright) |

---

## 9. Troubleshooting

**`MODEL.RUNTIME_UNAVAILABLE`** — planning or scene matching fails with "model runtime unavailable". Cause: no local endpoint reachable (`WENGAO_LOCAL_MODEL_ENDPOINT`, or the 18888→11434 probe). Fix: start your local runtime, e.g.
`llama-server -m /path/to/qwen-model.gguf --port 18888 --host 127.0.0.1 -c 4096 -np 1 --flash-attn on -ngl 99`.

**`PAGE_RANGE_UNSATISFIED`** — material generation rejected, no deck produced. Cause: the paginated result fell outside the **10—20** page contract. Fix: supply material with 7—10 clear core sections and enough factual sentences (40+ recommended).

**`LAYOUT.HARD_GATE_FAILED`** — verification reports a layout gate failure. Cause: insufficient contrast, font size below the role minimum, or missing native playback evidence. Fix: read the `issues` list in `<job>/layout_gate_runs/*/layout_gate.json`, complete the recording, or adjust via a controlled local edit.

**`SCENE.PHOTO_UNEXPECTED_CROP`** — scene gate blocks delivery. Cause: the image was stretched or cropped when placed. Fix: keep `contain` placement so the aspect ratio is 100% preserved.

**`SCENE.PEXELS_API_KEY_MISSING`** — because the workbench process was started without `PEXELS_API_KEY`. Fix: set it in the environment **of the process that starts the workbench** and restart; without it, the keyless Commons/Openverse sources are still tried (see §4.3).

**CJK render smoke test fails during install** — no LibreOffice or no usable CJK font. Fix: install LibreOffice (7.6+ / 24.x) and ensure a CJK font exists; the package ships only a `fonts.conf` resolution rule and **no font files**.

---

## 10. Capabilities and limitations (no "as planned" padding)

**Exists in code and is statically readable** (modules under `wengao_ppt/`; end-to-end status below):

- Material extraction and fact freezing: `extractors.py`, `source_fidelity.py`, `label_extract.py`;
- Plan → build → validate → export chain: `runner.py`, `checks.py`, `language_support.py`, `semantic_scenes.py`, `scene_images.py`, `scene_model.py`, `visual_plan.py`, `render.py`;
- `.docx`/`.pptx` parsing and controlled editing: `parsers_xml.py`, `extractors.py`, `local_edit.py`, `cleanup.py`, `page_catalog.py`;
- Theming and typography: `theme.py`, `refine.py`, `visual.py`, `visual_fixes.py`, `backdrop.py`, `readability.py`, `glyphcheck.py`, `fontenv.py`, `tables.py`, `charts.py`, `repair.py`;
- Incident ledger (append-only, hash-bound): `incident.py`;
- User-level install/upgrade/rollback: `install.py` (copy exclusions plus fail-closed verification).

**Measured open gaps** (do not treat as done):

1. 26-language real workbench chain **0/26**; 50 real decks **0/50** (no usable local model, no LibreOffice here).
2. Layout gates HG01—HG12: the candidate only ships the policy contract `contracts/layout_hard_gate_policy.json`; there is no HG implementation in `checks.py` — the gates currently run on the supervisor side.
3. P2/P5/P10 layout defects, native playback gate, 29 alpha objects: require real render/decode evidence, no renderer available here.
4. Greyscale engraving detection: without a model it cannot be separated reliably from black-and-white photos; no heuristic is applied, to avoid false kills. Kept as a known limitation.
5. The five AI-generated images lack verifiable provider/model/job-id records and carry no AI provenance metadata → `LICENSE-MATRIX.md` BLK-02.
6. Redistribution rights for the bundled Pexels samples are not cleared → BLK-03; they are excluded from the sanitized release copy by default.

---

## 11. Version history

| Version / candidate | Date | Changes | Evidence |
| :--- | :--- | :--- | :--- |
| `wengao-visual-v13-material-fix` (this candidate) | multi-round since 2026-10-20 | Language layer: Korean-han misclassification, URLs classified as Portuguese, mixed zh/en misread, zero recall for sw/tr. Imagery layer: candidates silently dropped behind proxy fake-IP, misleading empty-failure message, shared error codes, too-short timeout, zero hit rate for long phrases, line-art accepted as photo. Install layer: `copytree` without exclusions copied `jobs/ reports/ wb_loopback_data/` into `versions/<label>/` | Delivery note `00-交付说明.md`; test baseline 181 → 221 passed |
| P2 licensing and release cleanup (this round) | 2026-10-24 | Source inventory + license matrix, `THIRD-PARTY-NOTICES.md`, release sanitizer/scanner tooling, bilingual README and SKILL.md metadata | `reports/p2-license-release-20261023/` |
| Product version | — | `0.1.0` (`wengao_ppt/__init__.py`) | `--version` verified |

---

## 12. Third-party licenses

See `THIRD-PARTY-NOTICES.md` for the obligations at file level.

- **Upstream core code**: based on luobo-ppt, **MIT** (`vendor/luobo-ppt/LICENSE`).
- **Icons**: Lucide Icons, **ISC** — the file actually lives at `vendor/luobo-ppt/assets/LICENSE-lucide.txt` (an earlier document pointed at the wrong path `vendor/luobo-ppt/LICENSE-lucide.txt`; corrected here).
- **Python dependencies**: Pillow / lxml / openpyxl / pypdf / pypdfium2 / python-docx / python-pptx — license identifiers and version sources-of-truth in `LICENSE-MATRIX.md` §2.
- **Scene photos**: bundled Pexels samples must **not** be redistributed as an image pack; they are excluded from the sanitized release copy by default (see `ASSET-EXCLUSIONS.md` inside that copy).
- **Own code license**: **pending owner decision**; there is no `LICENSE` file yet, therefore nothing may be published yet.

---

## 13. Pre-release warnings

This candidate is **not** ready to publish. The following require the **project owner** (never an agent signing on their behalf or performing account steps):

1. Choose and add the own-code `LICENSE`; fill the `license` field in README / SKILL.md / SkillHub metadata.
2. Decide the fate of the five AI-generated images: supply generation evidence plus AI labelling, or replace/remove them.
3. Confirm whether bundling Pexels photos is permitted (terms-of-service / legal call).
4. Decide whether `vendor/luobo-ppt` ships, and whether to re-verify from the GitHub source first (the vendored git remote currently points at a local bundle — BLK-04).
5. Settle the dependency version pinning source of truth (requirements comment / local install / PyPI latest — BLK-06).
