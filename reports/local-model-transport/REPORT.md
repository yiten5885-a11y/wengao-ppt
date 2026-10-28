# 本机语义模型传输层修复报告

- 任务标识：WENGAO-LOCAL-MODEL-TRANSPORT
- 工作目录：`~/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix`
- 报告日期：2026-10-28
- 状态词按 `AGENTS.md` §3 使用（`NOT_RUN` / `NOT_EVALUABLE` / `静态核读` / `行为已测` / `发现缺陷` / `修复已验证` / `阶段通过`）

## 1. 结论

| 项 | 状态 |
| --- | --- |
| 故障原因定位（代码层） | `行为已测`（旧代码上用假服务复现出原始英文异常与 `provider=ollama-local`） |
| 传输层缺陷修复 | `修复已验证`（11 个新用例：旧代码全部失败，新代码全部通过） |
| 开跑前预检（fail fast） | `修复已验证`（真实重跑 `job-3afdd5d88bd9` 两次：3.45 s / 4.74 s 内以 `MODEL.VISION_RUNTIME_UNAVAILABLE` 停止） |
| 全量回归 | `阶段通过`（597 passed，0 failed；41–43 个 error 全是与本改动无关的环境钩子，见 §4.2） |
| 验收第 6 项（视觉可用后重跑至 DONE） | `NOT_EVALUABLE`（前置条件是用户侧环境决定，见 §8） |

一句话：**"读响应阶段被对端掐断"这类异常现在被当成传输故障处理（中文 detail、带端点、带重试次数、provider 按真实端点标注），并且不再盲重试——每次重试前先等端点恢复健康；同时把"视觉服务没启动"从"跑 5 分钟后崩在深处"提前到"开跑前 3 秒内说清楚"。**

## 2. 改动文件清单

| 文件 | 改动 |
| --- | --- |
| `wengao_ppt/scene_model.py` | 新增 `_post_json`（统一 HTTP/传输异常处理，404 换协议、`HTTPError` 读完 body 必 `close()`）；协议缓存 `_ENDPOINT_PROTOCOL` + `reset_endpoint_protocol_cache()`；`_call_endpoint_once` 改为按缓存协议单次调用；`SceneModelError(provider=...)` + `as_record()` 不再默认 `ollama-local`；`_probe_endpoint_health` / `_model_health_record`；重试策略（10 s / 30 s 退避 + 健康门 60 s）；`preflight_scene_models()`；本机视觉调用同步补异常分类与 provider 标注；云端调用补 `cloud:域名` 标注 |
| `wengao_ppt/semantic_scenes.py` | `build_plan()` 在调用任何模型之前执行 `preflight_scene_models()`（仅在未注入 `model_call` 的真实运行中），预检健康摘要写入 `plan["model_health"]` |
| `wengao_ppt/runner.py` | `semantic_scenes` DONE 阶段 detail 增加 `model_health` 字段 |
| `workbench/index.html` | "场景模型错误"一行增加 provider / 端点 / 重试次数，并按 `next_actions` 渲染"可以这样处理" |
| `contracts/error_codes_v1.json` | 新增 `MODEL.VISION_RUNTIME_UNAVAILABLE`（SF10 / BLOCKING，含 `required_evidence`）+ 一条 amendment |
| `tests/test_local_model_transport.py` | 新增 11 个用例（假 HTTP 服务，可编排"断开/挂住/404/正常"） |
| `tests/test_scene_model_timeout.py` | 更新 1 处 provider 断言：`ollama-local` → `local:unknown` |
| `tests/test_model_call_chain_diagnostics.py` | 更新 1 处 provider 断言：`ollama-local` → `local:127.0.0.1:11435` |
| `tests/test_scene_generation_identity_prompt.py` | 该测试类的注入假调用没有真实端点，把退避压成 0，避免白等 40 秒（断言语义不变） |

未改动：`~/.wengao_ppt/cloud_model.json`、`~/.codex/*`、`~/.venvs/mlx-dspark`、`~/Library/Application Support/MLXDSpark`、云端调用路径（`_call_cloud_text_once` / `_call_cloud_vision_once` / `_cloud_post_json` 的重试语义）、任何门禁阈值、任何语义规划/复核规则、`jobs/` 下既有任务目录。

## 3. 关键行为变化

1. **异常分类**：`http.client.RemoteDisconnected` / `IncompleteRead` / `ConnectionResetError` / `ssl.SSLError` 统一归 `MODEL.TRANSPORT_ERROR`，detail 为中文并带端点与协议路径：
   `本机语义模型连接被中断（RemoteDisconnected，端点 127.0.0.1:18088 /v1/chat/completions）。`
2. **协议缓存**：首次成功的协议（`ollama` / `llama` / `openai`）按端点记住，之后不再重复撞两次 404；缓存协议返回 404 时清缓存重新探测。
3. **重试策略**：只对 `MODEL.TRANSPORT_ERROR` 重试，最多 3 次，退避 10 s / 30 s（常量 `_LOCAL_TRANSPORT_BACKOFF_S`）；每次重试前先探测 `/health`（11434 用 `/api/tags`），每 5 s 一次、最多 60 s，仍不健康则停止并说明"60 秒内未恢复"。`MODEL.REQUEST_TIMEOUT` 从可重试集合中移除（900 秒预算下重试三遍等于白等 45 分钟）。
4. **诊断**：每次尝试写入 `diagnostics.attempts`（阶段、次数、根因类名、耗时、端点），最终失败 detail 末尾加"（已重试 N 次）"；重试后成功也会在 meta 里留 `transport_retry`。
5. **供应方标注**：本机调用标 `local:host:port`，云端标 `cloud:域名`；没有任何标注时只报 `local:unknown`，不再猜成 `ollama-local`。

## 4. 测试

### 4.1 新增用例（`tests/test_local_model_transport.py`，11 个）

| # | 用例 | 覆盖的验收条款 |
| --- | --- | --- |
| 1 | `test_closed_connection_is_chinese_transport_error_with_endpoint` | 第 3 节 1 |
| 2 | `test_two_disconnects_then_success_recovers_and_records_attempts` | 第 3 节 2（0.54 s，< 5 s） |
| 3 | `test_persistent_disconnect_reports_retry_count` | 第 3 节 3 |
| 4 | `test_timeout_is_not_retried` | 第 3 节 4 |
| 5 | `test_unrecovered_health_stops_retrying` | 第 3 节 5 |
| 6 | `test_second_call_reuses_the_successful_protocol` | 第 3 节 6 |
| 7 | `test_probe_responses_are_closed` | 第 3 节 7 |
| 8 | `test_vision_endpoint_missing_fails_before_any_text_planning` | 第 3 节 8 |
| 9 | `test_build_plan_fails_before_planning_when_vision_is_missing` | 第 3 节 8（真实入口 `build_plan`，且断言文本端点一个 POST 都没收到） |
| 10 | `test_text_endpoint_missing_fails_fast_with_runtime_unavailable` | 第 1.4 节文本预检 |
| 11 | `test_memory_pressure_is_recorded_but_not_blocking` | 第 1.4 节第 3 条（只记录不阻断） |

### 4.2 修改前后结果

| 时点 | 命令 | 结果 |
| --- | --- | --- |
| 修改前（基线，第 1 次） | `./.venv/bin/python -m pytest -q -p no:cacheprovider tests` | 586 passed，33 errors，331 subtests，103 s |
| 修改前（基线，第 2 次，同命令复跑） | 同上 | 577 passed，44 errors，331 subtests，93 s |
| 修改后（第 1 次） | `./.venv/bin/python -m pytest -q -p no:cacheprovider tests --basetemp=/tmp/wb_pytest_run` | 597 passed，41 errors，0 failed，331 subtests，146 s |
| 修改后（最终代码，第 2 次） | `./.venv/bin/python -m pytest -q -p no:cacheprovider tests --basetemp=/tmp/wb_pytest_run2` | **597 passed，43 errors，0 failed，331 subtests，151 s** |

- passed 增加 11 个，正好是新增用例数；**没有任何 FAILED**（两次修改后运行都是 0 failed）。
- 41 / 43 个 `error` 全部是**用例收尾阶段**的外部安全钩子 `SystemExit: 1`（`[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]`：测试清理的临时目录超过 50 个文件时被本机 `safe-delete` 钩子拦截）以及 pytest 临时基线目录 `pytest-of-root` 冲突。这类 error 在修改前的两次基线里分别出现 33 / 44 个，数量随临时目录复用情况浮动，与本次改动无关；用例本体一个都没有失败。
- 指定文件单独运行：`tests/test_cloud_transport_retry.py` 5 passed（验收第 3 节第 9 条，原样通过）；`tests/test_local_model_transport.py` 11 passed。

### 4.3 新用例在"修改前代码"上确实失败

把 `wengao_ppt/scene_model.py`、`wengao_ppt/semantic_scenes.py` 还原为 `HEAD` 版本后复跑：

```
ERROR tests/test_local_model_transport.py
E   ImportError: cannot import name 'preflight_scene_models' from 'wengao_ppt.scene_model'
1 error in 0.21s
```

为看到逐条行为差异，另用一份去掉新接口依赖的临时副本（仅保留 7 个行为用例）在旧代码上运行：

```
FAILED RemoteDisconnectTests::test_closed_connection_is_chinese_transport_error_with_endpoint
FAILED RemoteDisconnectTests::test_persistent_disconnect_reports_retry_count
FAILED RemoteDisconnectTests::test_timeout_is_not_retried
FAILED RemoteDisconnectTests::test_two_disconnects_then_success_recovers_and_records_attempts
FAILED RemoteDisconnectTests::test_unrecovered_health_stops_retrying
FAILED ProtocolCacheTests::test_probe_responses_are_closed
FAILED ProtocolCacheTests::test_second_call_reuses_the_successful_protocol
7 failed in 3.77s
```

同一份假服务在旧代码上的原始表现（复现生产事故现场）：

```
OLD_RAW_EXCEPTION http.client.RemoteDisconnected: Remote end closed connection without response
OLD_IS_SCENE_MODEL_ERROR False
OLD_PAGE_RECORD code=MODEL.TRANSPORT_ERROR provider=ollama-local detail='Remote end closed connection without response'
```

即：旧实现把未包装的 `http.client.RemoteDisconnected` 直接抛出去，页面因此显示英文原文，`provider` 被写成 `ollama-local`。新实现下同一场景得到：
`MODEL.TRANSPORT_ERROR / local:127.0.0.1:PORT / 本机语义模型连接被中断（RemoteDisconnected，端点 127.0.0.1:PORT /api/generate）。`

## 5. 验收第 4 项：真实重跑（实际输出）

按 §6 步骤重启工作台（`--port 8823`），提交同一原稿与主题。共重跑两次（第二次是补上 `text_model_health` 之后的最终代码），两次结论一致：

```
# 第 1 次：2026-10-28T11:13:11.27 POST → 11:13:14.73 semantic_scenes FAILED（3.45 s）
# 第 2 次：2026-10-28T11:22:03    POST → 11:22:07.77 semantic_scenes FAILED（4.74 s）
POST /api/make {"source":"workbench_uploads/28911bf3__Token_.pptx","theme":"wengao-visual-dark"}
→ accepted=true, job_id=job-3afdd5d88bd9
```

`jobs/job-3afdd5d88bd9/job.json` 阶段记录摘录（第 2 次，最终代码）：

```json
{"stage": "semantic_scenes", "status": "RUNNING", "at": "2026-10-28T11:22:07.425215+08:00",
 "detail": {"rules_version": "2.22.0", "note": "按全稿语义规划具体场景，检索授权图片并安全放置。"}}
{"stage": "semantic_scenes", "status": "FAILED", "at": "2026-10-28T11:22:07.766268+08:00",
 "detail": {"code": "MODEL.VISION_RUNTIME_UNAVAILABLE",
            "detail": "图片语义复核需要本机视觉模型（127.0.0.1:8080 或 11434）或启用云端视觉，当前都不可用。",
            "model_error": {"code": "MODEL.VISION_RUNTIME_UNAVAILABLE",
                            "provider": "local:127.0.0.1:8080",
                            "detail": "图片语义复核需要本机视觉模型（127.0.0.1:8080 或 11434）或启用云端视觉，当前都不可用。",
                            "diagnostics": {"endpoint": "127.0.0.1:8080",
                                            "phase": "预检（视觉）",
                                            "probed_endpoints": ["127.0.0.1:8080", "127.0.0.1:11434"],
                                            "next_actions": ["启用云端视觉：在工作台「云端模型」设置里打开视觉模型（原稿图片会发往该云端服务商，需你显式同意）",
                                                             "启动本机视觉服务（mlx_vlm，端口 8080）"],
                                            "text_model_health": {"endpoint": "127.0.0.1:18088",
                                                                  "status": "ok",
                                                                  "model": "Qwen3.8-27B-4bit",
                                                                  "memory_pressure": "none",
                                                                  "warning_codes": ["load_note", "memory_guard"]}}}}}}
```

- 提交到失败：**3.45 s / 4.74 s**（旧行为是 `RUNNING` 10:14:28 → `FAILED` 10:19:32，即 5 分 4 秒后才崩）。
- `semantic_scenes` 的 RUNNING→FAILED 间隔 0.32–0.34 秒：失败发生在预检，**没有发出任何文本规划请求**（第 8/9 号用例在测试层也断言了这一点）。
- 视觉预检失败时也带上了文本端点的健康摘要（`text_model_health`）：这次 `memory_pressure` 恰好是 `none`（只剩 `load_note` 与 `memory_guard`），说明该字段反映的是当时的真实状态，不是写死的。
- 页面所需字段已具备：`GET /api/make/job-3afdd5d88bd9` 返回 `runner.semantic_scene_stage.model_error`（含 `provider` / `diagnostics.endpoint` / `diagnostics.next_actions`），`workbench/index.html` 正是从该路径渲染；页面脚本已用 `node --check` 校验语法。

## 6. 验收第 5 项：故障注入（实际输出）

不碰真实服务：本机起一个只做转发的假端点（`127.0.0.1:<随机端口>` → `127.0.0.1:18088`），**前 2 个 POST 直接断开、不回包**，之后正常转发。客户端用 `WENGAO_LOCAL_MODEL_ENDPOINT` 指向它。

```
PROXY http://127.0.0.1:54394 -> http://127.0.0.1:18088 (drop first 2 POSTs)
RESULT ok={'ok': True} elapsed=229.7s
META transport_retry={"attempts": 3, "phase": "故障注入（规划）",
  "failed_attempts": [
    {"attempt": 1, "code": "MODEL.TRANSPORT_ERROR", "exception": "RemoteDisconnected", "endpoint": "127.0.0.1:54394"},
    {"attempt": 2, "code": "MODEL.TRANSPORT_ERROR", "exception": "RemoteDisconnected", "endpoint": "127.0.0.1:54394"}]}
COUNTERS posts=5 dropped=2 forwarded=5 health_gets=2
```

- 第 1、2 次尝试各被断开一次 → 每次断开后先探测 `/health`（`health_gets=2`）→ 第 3 次尝试经真实 mlx-dspark（`Qwen3.8-27B-4bit`）成功返回。
- 229.7 秒 = 10 s + 30 s 退避 + 真实 27B 在本机内存压力下的首包/生成时间（本次真实调用约 190 秒；本机 `/health` 自报 `memory_pressure WARNING`，见 §8）。
- 范围说明：这条证据验证的是 `semantic_scenes` 的**模型调用层**（`call_local_model` → `_call_endpoint_once` → `_call_json_payload_with_transport_retry`）在同模型、同端点上自动恢复；整单 `semantic_scenes` 跑到 DONE 仍受 §8 的视觉端点前置条件限制（第 6 项）。

## 7. 验收第 6 项：视觉可用后重跑 → `NOT_EVALUABLE`

未执行。原因：本机 `127.0.0.1:8080`（mlx_vlm）与 `127.0.0.1:11434`（Ollama）都没有监听，`~/.wengao_ppt/cloud_model.json` 中云端为 `enabled=false`；按 `AGENTS.md` §5 与本次任务 §2，开启云端或启动视觉服务属于用户决定，代理不代做（云端还涉及原稿图片出境）。

用户处置后可直接复跑（任一即可）：

```bash
# A. 启用云端视觉（工作台「云端模型」设置里打开，需显式同意）
# B. 或启动本机视觉服务：
mlx_vlm.server --port 8080            # 具体启动参数按本机 mlx_vlm 版本
# 然后重跑：
cd ~/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix
./.venv/bin/python workbench/server.py --port 8823 &   # 若未运行
curl -s --noproxy '*' -X POST -H 'Content-Type: application/json' \
  -d '{"source":"workbench_uploads/28911bf3__Token_.pptx","theme":"wengao-visual-dark"}' \
  http://127.0.0.1:8823/api/make
curl -s --noproxy '*' http://127.0.0.1:8823/api/make/job-3afdd5d88bd9
```

复跑后应看到：`semantic_scenes` 的 DONE detail 里带 `model_health`。本次实测该字段的三种取值都出现过（说明它读的是端点实时状态）：`memory_pressure: "attention"`（10:53 探测，swap 8.9 GB 时）、`"none"` 但 `warning_codes: ["load_note","memory_guard"]`（11:22 重跑时）。若仍断连，失败信息会带每次尝试的阶段、根因类名、耗时、端点，可直接判断是否为内存压力。

## 8. 需要用户处理的环境问题（代理未动手）

1. **本机视觉服务缺失（当前唯一硬阻塞）**：二选一——启用云端视觉，或启动本机 `mlx_vlm`（端口 8080）。不处理时，云端关闭的每一次运行都会在 3 秒内以 `MODEL.VISION_RUNTIME_UNAVAILABLE` 停止（这是设计行为，不再是跑 5 分钟后崩）。
2. **内存压力**：`/health` 的 `warnings` 随当时状态变化——10:53 探测为 `memory_pressure: WARN (8.7–8.9 GB swapped)`、`memory_guard` 累计释放 16 次；11:22 重跑时只剩 `load_note` 与 `memory_guard`（`memory_pressure: none`）。另：`context_window=262144` 的 KV cache 理论上还要 ~16 GB。可选处置：按 mlx-dspark 自己的建议用 `--context-window 114688` 重启，或在跑工作台任务时关闭其他占内存的应用。
3. **不要同时用 Codex 本机配置和工作台任务**：`~/.codex/mlx-dspark.config.toml` 也指向 18088，两者争抢同一个 27B 与同一块 GPU 内存；本次故障注入里单次真实调用就用了约 190 秒。

## 9. 取舍与残留风险（如实记录）

1. **`MODEL.VISION_RUNTIME_UNAVAILABLE` 只登记在 `contracts/error_codes_v1.json`**（含 amendment）。`contracts/errors/*.json` 是按历史模块（`SCENE`/`LAYOUT`/`PIPE`/`RESEARCH`/`TRANSITION`）划分的命名空间文件，没有 `MODEL` 命名空间文件；把 MODEL 码塞进 `scene.json`（W1 归属）会破坏命名空间约定。`scripts/aggregate_error_codes.py` 校验仍为"5 个命名空间 / 45 码 / 无重复"。
2. **`provider=ollama-local` 不再作为默认值**：未标注来源的 `MODEL.*` 报 `local:unknown`。为此更新了两处既有断言（`test_scene_model_timeout.py`、`test_model_call_chain_diagnostics.py`）——它们固化的正是"一律猜 Ollama"这条被本次任务否定的行为。
3. **预检只在未注入 `model_call` 的真实运行中执行**：注入假调用的测试/夹具根本不碰端点，对它们做端点预检会让单测依赖本机服务。
4. **`call_local_model` 的 18888 回退保留未动**（既有行为，仅 loopback 且需 `WENGAO_FALLBACK_MODEL_ENDPOINT` 显式配置；任务 §2 要求"保持现有不静默切换供应方规则"）。它不会切到云端。
5. **计划哈希包含 `model_health`**：预检的健康快照进入 `scene_plan.json`，`plan_sha256` 因此随环境状态变化。这是有意的（同一份稿在不同环境下应产生不同的可审计计划），但会让"跨机对比 plan_sha256"失去意义。
6. 页面未做端到端截图核验（`行为已测` 只到接口层：已确认接口回传 `model_error.diagnostics.endpoint` 与 `next_actions`，且 `index.html` 从 `runner.semantic_scene_stage` 读取同一对象）；渲染逻辑改动仅新增字段拼接与一个 `next_actions` 列表块。
