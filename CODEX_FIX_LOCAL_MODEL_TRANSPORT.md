# 修改指令：本机语义模型"Remote end closed connection without response"导致整单失败

- 任务标识：WENGAO-LOCAL-MODEL-TRANSPORT
- 工作目录：`~/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix`
- 失败任务：`job-3afdd5d88bd9`，主题 `wengao-visual-dark`，原稿 `workbench_uploads/28911bf3__Token_.pptx`
- 页面现象：渲染候选页面失败。`semantic_scenes` 阶段从 10:14:28 运行到 10:19:32 后失败，报错为 `MODEL.TRANSPORT_ERROR · Remote end closed connection without response`，`provider` 显示为 `ollama-local`。

---

## 0. 已查明的原因（2026-10-28 实测，状态：行为已测）

### 0.1 环境：这次走的是本机模型，不是云端

- `~/.wengao_ppt/cloud_model.json` 在 2026-10-27 22:02 被设为 `enabled: false`。因此文本规划、图片复核、生图全部走本机。
- 文本规划端点：`http://127.0.0.1:18088`，是 mlx-dspark 0.20.3，运行 `Qwen3.8-27B-4bit`（PID 1115，10-25 启动，进程没有崩溃过）。
- 该机器内存压力很大：
  - swap 已用 15.0 GB / 16 GB；
  - mlx-dspark 的 `/health` 自报 `memory_pressure WARN`，内存守卫已累计释放 16 次缓存，日志里多次出现 `pressure CRITICAL`；
  - 它自己的建议是加 `--context-window 114688`。
- Codex 本机配置 `~/.codex/mlx-dspark.config.toml` 也指向同一个 18088。Codex 与工作台同时使用时会争抢同一个 27B 模型和内存。
- 复现结果：用这份原稿的真实规划提示词（9160 字符），按代码同样的方式直接调用 18088 两次，**两次都成功**，每次约 100 秒。说明断连是**偶发的**，与当时的内存压力、并发有关，不是每次必现。

### 0.2 代码缺陷 1：本机调用不捕获"连接被对端关闭"

- 位置：`wengao_ppt/scene_model.py` 的 `_call_endpoint_once`（约第 203–354 行）。
- 调用顺序：先 `POST /api/generate`（404），再 `POST /completion`（404），最后 `POST /v1/chat/completions`。
- Python 的 urllib 只把"发送请求"阶段的错误包装成 `URLError`。"读取响应头"阶段的 `http.client.RemoteDisconnected`、`ConnectionResetError` 等会**原样抛出**。
- 这三层 `try` 只捕获了 `HTTPError`、`URLError`、`TimeoutError` 和 `JSONDecodeError`，所以原始英文异常一路冒到 `_call_json_payload_with_transport_retry`，再冒到 `runner.py:1714` 的兜底分类，导致：
  - 页面显示原始英文 `Remote end closed connection without response`，没有说明是哪个端点、哪个阶段、重试了几次；
  - `provider` 被硬写成 `ollama-local`（`SceneModelError.as_record`，约第 47 行），实际是 mlx-dspark，误导排查。

### 0.3 代码缺陷 2：重试太急，且超时也会被重复等待

- `_call_json_payload_with_transport_retry`（约第 1152 行）：最多 3 次，间隔只有 2 秒和 4 秒。这对于"正处于内存压力、刚释放缓存"的本机服务太短，三次很可能都落在同一个高压窗口里。
- `_MODEL_TRANSIENT_CODES` 包含 `MODEL.REQUEST_TIMEOUT`，单次超时预算是 900 秒，所以最坏情况会等待 45 分钟才失败。
- 每次调用都重复试探 `/api/generate`、`/completion` 两个 404，而且 404 响应没有关闭。复现时看到客户端留下了 `CLOSE_WAIT` 连接。

### 0.4 隐藏的第二个阻断：本机图片复核服务没有运行

- 云端关闭后，图片语义复核走 `_vision_loopback_endpoint()`，候选是 `127.0.0.1:8080`（mlx_vlm）和 `127.0.0.1:11434`（Ollama）。实测**两个都没有在监听**。
- 也就是说，即使文本规划这次没断，跑到图片复核时也会以 `ConnectionRefusedError` 失败。现在的流程要先跑完几分钟的文本规划才会发现这个问题。

---

## 1. 改哪里、怎么改

### 1.1 `wengao_ppt/scene_model.py` → `_call_endpoint_once`

1. 把三层请求合并成一个内部函数 `_post_json(opener, url, payload, timeout_s)`，统一处理异常：
   - `HTTPError`：读完 body 后**必须 `exc.close()`**，避免留下 `CLOSE_WAIT`。404 交给上层选择下一种协议，其他状态码按现有规则分类。
   - `URLError`：
     - 原因是超时 → `MODEL.REQUEST_TIMEOUT`；
     - 其他原因 → `MODEL.TRANSPORT_ERROR`。
   - **新增**捕获 `http.client.HTTPException`（含 `RemoteDisconnected`、`IncompleteRead`）、`ConnectionError`、`ssl.SSLError`，统一归为 `MODEL.TRANSPORT_ERROR`。
   - `MODEL.TRANSPORT_ERROR` 的 detail 写成中文：`本机语义模型连接被中断（RemoteDisconnected，端点 127.0.0.1:18088 /v1/chat/completions）`。不得把原始英文异常文本直接当 detail。
2. **协议缓存**：在模块级字典 `_ENDPOINT_PROTOCOL = {endpoint: "ollama" | "llama" | "openai"}` 中记录首次成功使用的协议，之后直接调用该协议，不再重复试探 404。
   - 端点变化或缓存协议返回 404 时，清空缓存并重新探测。
   - 测试里要能重置这个缓存。
3. 读取响应体时，`response.read()` 抛出的 `IncompleteRead` / `ConnectionResetError` 同样归为 `MODEL.TRANSPORT_ERROR`。

### 1.2 `wengao_ppt/scene_model.py` → `SceneModelError` 与 provider 标注

- `SceneModelError.__init__` 增加可选参数 `provider: str | None`。
- `as_record()` 优先使用传入的 provider，没有时才按现有规则推断。
- 本机文本调用传入 `local:<host:port>`，例如 `local:127.0.0.1:18088`；本机视觉调用同理；云端调用传入 `cloud:<域名>`。
- 不得再把 mlx-dspark 或 llama.cpp 标成 `ollama-local`。

### 1.3 `wengao_ppt/scene_model.py` → `_call_json_payload_with_transport_retry`

- 只对 `MODEL.TRANSPORT_ERROR` 重试，最多 3 次。退避改为 **10 秒、30 秒**，并做成常量，测试里可以注入。
- 每次重试前先探测该端点的 `/health`（11434 用 `/api/tags`）：每 5 秒探测一次，最多等 60 秒，等到返回 200 再发请求。60 秒内仍不健康就停止重试，直接失败，detail 写明"本机模型服务 60 秒内未恢复"。
- `MODEL.REQUEST_TIMEOUT` **不再重试**，从 `_MODEL_TRANSIENT_CODES` 中移除。理由：900 秒超时说明请求本身过重，重试三遍只会让用户白等 45 分钟。
- 每次尝试都写入诊断信息，复用 `SceneModelError.diagnostics`，内容包括：阶段名（规划 / 逐页复核 / …）、第几次、异常类名、耗时、端点。最终失败时 detail 末尾加上"（已重试 N 次）"。
- 不得换模型、不得换端点、不得切换到云端（保持现有"不静默切换供应方"的规则）。

### 1.4 `wengao_ppt/semantic_scenes.py` / `wengao_ppt/runner.py` → 开跑前预检（fail fast）

在 `semantic_scenes` 阶段开始、调用任何模型之前，新增一次预检 `preflight_scene_models()`，放在 `scene_model.py` 中：

1. **文本端点**：
   - 云端文本可用 → 通过；
   - 否则探测 `_loopback_endpoint()` 的健康接口，3 秒内无响应 → `MODEL.RUNTIME_UNAVAILABLE`。
2. **视觉端点**：本次运行确实需要图片复核时才检查，判断条件沿用现有逻辑，不得改变"是否复核"的规则。
   - 云端视觉可用 → 通过；
   - 否则探测 `_vision_loopback_endpoint()`，不可用 → 新错误码 `MODEL.VISION_RUNTIME_UNAVAILABLE`，detail 为："图片语义复核需要本机视觉模型（127.0.0.1:8080 或 11434）或启用云端视觉，当前都不可用。"
3. 若 mlx-dspark 的 `/health` 返回了 `warnings` 中的 `memory_pressure`，**不阻断**，但要把它写进 stage detail 的 `model_health` 字段，便于事后排查。
4. 新错误码登记到 `contracts/error_codes_v1.json` 以及 `contracts/errors/` 下对应的位置（按项目现有登记方式）。在 `next_actions` 中给出两条：
   - "启用云端视觉"；
   - "启动本机视觉服务（mlx_vlm，端口 8080）"。

### 1.5 工作台页面（`workbench/index.html` 中"场景模型错误"一行）

- 显示中文 detail、端点、重试次数，不显示原始英文异常。
- `MODEL.VISION_RUNTIME_UNAVAILABLE` 显示上面两条 `next_actions`。

---

## 2. 边界（不能做的事）

- **不改 mlx-dspark、Ollama、Codex 的任何文件或配置**，包括 `~/.venvs/mlx-dspark`、`~/.codex/*`、`~/Library/Application Support/MLXDSpark`（AGENTS.md §5）。环境问题只能写进报告，由用户决定。
- 不改 `~/.wengao_ppt/cloud_model.json`，不擅自开启云端。
- 不动云端调用路径（`_call_cloud_text_once` / `_call_cloud_vision_once` / `_cloud_post_json`），那里 10-27 已经修过 SSLEOFError 并验证。
- 不改任何门禁阈值，不改语义场景的规划、复核规则，不把传输失败记成"场景不成立"或 PASS。
- 不自动换模型、换端点、换供应方。
- `findings.json` / `validation.json` / `job.json` 只能由真实运行产生，不得手写（AGENTS.md §2）。
- 不删除、不移动 `jobs/` 下的既有任务目录。

---

## 3. 测试（新增 `tests/test_local_model_transport.py`）

用本机临时端口起一个**假的 HTTP 服务**（在线程里运行 `socketserver`），可以按脚本控制每个请求的行为：

1. **接受连接后直接关闭、不回包** → `_call_endpoint_once` 抛出 `SceneModelError`：code 为 `MODEL.TRANSPORT_ERROR`，detail 是中文、包含 `RemoteDisconnected` 和端点，`as_record()["provider"]` 是 `local:127.0.0.1:<port>`。
2. **前 2 次断开、第 3 次正常返回** → `_call_json_payload_with_transport_retry` 最终成功，诊断里记录了 2 次失败。退避和健康等待用注入的极小值，测试总耗时 < 5 秒。
3. **一直断开** → 3 次后失败，detail 含"已重试 3 次"。
4. **超时** → `MODEL.REQUEST_TIMEOUT` 只尝试 1 次，不重试。
5. **健康检查 60 秒内不恢复**（用注入的小值模拟）→ 停止重试，detail 含"未恢复"。
6. **协议缓存**：对 openai 协议的假服务连续调用 2 次，第 2 次不再请求 `/api/generate`、`/completion`（统计假服务收到的路径）。
7. **不泄漏连接**：404 试探之后，服务端统计到的连接都已被客户端关闭（没有 `CLOSE_WAIT` 残留）。
8. **预检**：视觉端口不可用且需要复核 → 在 1 秒内抛出 `MODEL.VISION_RUNTIME_UNAVAILABLE`，且没有发出任何文本规划请求。
9. **云端路径不受影响**：`tests/test_cloud_transport_retry.py` 原样通过。

全量测试：`./.venv/bin/python -m pytest -q -p no:cacheprovider tests` 必须全部通过（10-27 基线为 580 passed）。新增测试要在修改前的代码上失败、修改后通过，证明测试有效。

---

## 4. 怎么样才算改好了（验收标准，全部满足）

| # | 检查 | 通过条件 |
|---|---|---|
| 1 | 第 3 节测试 1–9 | 全部通过，并在旧代码上验证过会失败 |
| 2 | 全量测试 | 全部通过，数量 ≥580 |
| 3 | 页面报错文案 | 本机模型断连时显示中文、端点、重试次数；不再出现 `ollama-local` |
| 4 | 预检（真实环境，本机视觉服务未运行，云端关闭） | 重跑 `job-3afdd5d88bd9`（visual-dark），**10 秒内**以 `MODEL.VISION_RUNTIME_UNAVAILABLE` 停止，页面给出两条可操作建议；不得先跑几分钟规划再失败 |
| 5 | 故障注入（假服务冒充 18088，前 2 次断开后恢复） | 关闭 mlx-dspark 前须先征得用户同意；或者通过 `WENGAO_LOCAL_MODEL_ENDPOINT` 指向假服务，**不碰真实服务**。`semantic_scenes` 的模型调用阶段能自动恢复 |
| 6 | 真实重跑（用户启用云端视觉或启动 8080 之后） | `job-3afdd5d88bd9` 的 `semantic_scenes` 为 DONE。若仍断连，失败信息包含每次尝试的阶段、耗时、端点，可据此判断是否为内存压力 |
| 7 | 报告 `reports/local-model-transport/REPORT.md` | 包括：改动文件清单、测试前后结果、第 4/5/6 项的实际输出（job.json 的阶段记录摘录）、未解决的环境问题清单（见第 5 节）。状态词按 AGENTS.md §3 使用 |

---

## 5. 需要用户自己处理的环境问题（Codex 只写进报告，不要动手）

1. **本机视觉服务**：二选一。
   - 启用云端视觉：在工作台的云端设置里打开；
   - 或启动本机 mlx_vlm 视觉服务（端口 8080）。
   - 不处理的话，云端关闭时图片复核必然失败。
2. **内存压力**：swap 已用 15 GB。可以：
   - 按 mlx-dspark 自己的建议，用 `--context-window 114688` 重启它；
   - 或在跑工作台任务时关闭其他占内存的应用。
3. **不要同时用 Codex 本机配置**（`mlx-dspark.config.toml`，同样指向 18088）和工作台任务，两者会争抢同一个 27B 模型。

---

## 6. 重跑方法

```bash
cd ~/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix
kill $(pgrep -f "server.py --port 8823")
nohup env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
  ./.venv/bin/python workbench/server.py --port 8823 >> workbench_server_8823.log 2>&1 &
curl -s --noproxy '*' -X POST -H 'Content-Type: application/json' \
  -d '{"source":"workbench_uploads/28911bf3__Token_.pptx","theme":"wengao-visual-dark"}' \
  http://127.0.0.1:8823/api/make
# 查看结果：
curl -s --noproxy '*' http://127.0.0.1:8823/api/make/job-3afdd5d88bd9
# 阶段记录：jobs/job-3afdd5d88bd9/job.json 的 stages
# 本机模型健康：curl -s --noproxy '*' http://127.0.0.1:18088/health
# 本机模型日志：~/Library/Application Support/MLXDSpark/logs/serve.err.log（只读）
```
