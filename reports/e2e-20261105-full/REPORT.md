# 材料任务端到端复跑（2026-11-05）

目的：两个修复（HG13 说明卡兜底口径、云端视觉失败回退本机）此前只在**隔离根重跑
validate** 上验证过。生成链路（场景规划 → 视觉复核 → 结账）从未用修复后的代码
完整跑过一次。本目录记录这次复跑的原始结论，包括跑失败的那部分。

隔离数据根：`reports/e2e-20261105-full/`，不覆盖任何历史证据。
输入：`workbench_uploads/721cd441__V1.0.docx`（SHA256 `1b1a3780…`，与 00:19
那次失败任务同源）。
主题 `wengao-dark`（registry 1.3.0，config digest `0b0ef910…`），页数 12，
模型 `qwen3.8:27b-wb65536`，`--allow-text-card-fallback`。

> 未改动 `~/.wengao_ppt/cloud_model.json`（仍指向 Gemini）。这正是本次要验证的
> 真实条件：云端 402 已耗尽额度，本机 8B 在线。

---

## 1. 生成候选（from-material）

```
exit=0，1m07s
facts_frozen=511，plan_mode=chunked，plan_chunks=8，plan_slides=14
[pages] 10 → 12 页 (区间 10—20, in_range=True)
[pptx] …/out/721cd441__V1.0.generated.pptx slides=14 chart_points=12
precheck → job-08c2fe26cd1d（support_profile=P1_TEXT_LIMITED，real_renderer=available）
```

`--accept` 只做 precheck，明确不自动进入验收（`accepted_into_pipeline=false`），
与产品声明一致。

## 2. 第一次 run：视觉回退真实生效，但被单个场景拦下

```
exit≠0，gate=VISUAL.SCENE_UNRESOLVED
[vision-fallback] cloud=cloud:generativelanguage.googleapis.com \
                  code=MODEL.QUOTA_EXCEEDED -> local=127.0.0.1:8080   ×6
```

**回退按设计工作**：6 次云端 402 全部落到本机 8B，没有一次把整单判死。
（此前 00:19 的 mt-e5f3bcbf 正是在这里整单 FAILED。）

随后场景结账：

| 项 | 值 |
|---|---|
| 检出场景 | 5（`player_tapping_generator` / `player_receiving_reward` / `player_spending_diamonds` / `designer_typing_font_specs` / `designer_reviewing_ui_mockup`）|
| coverage_audit | PASSED，14 页，missing_scene_count=0 |
| 已配图 | 0 |
| 说明卡兜底 | 4 |
| 未解决 | 1：`designer_typing_font_specs` → `SCENE.GEN_IMAGE_PERMANENT_FAILURE` |
| plan_sha256 | `1dae0c71…` |

该场景的两条检索尝试写得清楚：图库回图但无一合格（`PROVIDER_ROWS_INELIGIBLE`，
逐项拒绝 3+91+1 条），转而用云端生图 → 该端点本次任务已判永久失败
（`GEN_IMAGE_PERMANENT_FAILURE`）。

### 缺陷③：`GEN_IMAGE_PERMANENT_FAILURE` 漏在「没图可用」政策之外

`NO_PHOTO_FOUND_REASONS` 收录了 9 个 `GEN_IMAGE_*` 码，唯独漏了这一个。
模块自己的注释把"生图不可用或不合格"明确列为**可安全跳过**（跳过不会插入任何
图）。漏登记的后果：用户已勾选兜底、5 个场景里 4 个都已正常兜底，只剩这 1 个
把整份稿子判 FAILED。

修法：
1. 该码补入 `NO_PHOTO_FOUND_REASONS`；
2. 把 build_plan 里内联的降级改写提为纯函数 `downgrade_no_photo_unresolved`，
   保留 `original_reason` 与 `query_attempts`（跳过 ≠ 无记录）；
3. 新增结构性不变量测试：`wengao_ppt` 里出现的每一个 `SCENE.GEN_IMAGE_*`
   都必须被该政策覆盖——新增生图失败码忘分类时测试直接失败。

许可、隐私、去重、复核失灵（`VISUAL_REVIEW_UNAVAILABLE`、`PRIVACY_REVIEW_REQUIRED`
等）一律不降级，仍然阻断。

## 3. 第二次、第三次 run：本机显存不足（环境）

```
gate=MODEL.HTTP_ERROR
Generation failed: [METAL] Command buffer execution failed:
Insufficient Memory (kIOGPUCommandBufferCallbackErrorOutOfMemory)
provider=local:127.0.0.1:18088，status 500，elapsed 61.99s
```

不是偶发：连续两次都在同一处失败。查到服务端指标：

```
27B 单次规划：prompt_tokens=2973，peak_memory_gb=26.94
（另有 8B 视觉服务常驻 ≈5GB；本机物理内存 32GB）
```

即 27B 在当前 `loaded_context_size=262144` 下自身就要 26.9GB，8B 再驻留必然
越界。这是**机器容量**问题，不是产品逻辑缺陷：换更大内存的机器、或把 27B 服务
的上下文降下来（属服务端启动参数，本次未改动用户环境），即可跑通。

## 4. 决策级复验（用第一次运行的真实计划数据）

`decision_replay.json` 由 `job-08c2fe26cd1d/job.json` 里首次运行的真实结论直接
计算，不是构造样本：

| | 修复前 | 修复后 |
|---|---|---|
| blocking | 1 | **0** |
| capacity/unplaced | 0 | **1**（`original_reason` 保留为 `GEN_IMAGE_PERMANENT_FAILURE`）|
| run 阶段 `scene_stage_verdict` | — | `blocks=false, detail_kind=resolved` |

## 5. 结论与边界

- 云端 402 → 本机回退：**真实链路生效**（6 次），且未改动用户配置。
- 缺陷③：**已修**，真实数据复验 blocking 1→0。
- 完整 run→validate→publish→export：**BLOCKED_ENV**（27B 与 8B 无法在同一台
  32GB 机器共存）。不以此宣告端到端通过。
- 同一轮中，`job-624391145aa3` 的 validate→publish→export 已在
  `reports/rerun-20261105-hg13/` 通过（那份是修复后代码 + 真实候选），本报告
  不重复声明。
- 未推远端、未改许可证、未改云端配置、未下载模型、未删除项目文件。

## 6. 复跑命令

```bash
P=…/wengao-visual-v13-material-fix ; ROOT="$P/reports/e2e-20261105-full"
cd "$P"
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY .venv/bin/python \
  -m wengao_ppt.cli --project-root "$ROOT" from-material \
  --input "$P/workbench_uploads/721cd441__V1.0.docx" \
  --pages 12 --model qwen3.8:27b-wb65536 --theme wengao-dark \
  --outdir "$ROOT/out" --accept
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY .venv/bin/python \
  -m wengao_ppt.cli --project-root "$ROOT" run \
  --job-id job-08c2fe26cd1d --allow-text-card-fallback --timeout 1800
```

（显存条件满足时，第二步之后继续 `validate` / `publish` / `export`。）
