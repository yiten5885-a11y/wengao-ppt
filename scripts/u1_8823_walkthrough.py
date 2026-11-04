"""U1 监督方 UI 全链复验（安装实例 8823）——HTTP 层证据采集。

覆盖：bootstrap 首屏（含耗时）→ meta → 上传原稿 → /api/make 创建任务并启动
run → 轮询至 run 完成 → validate → export → 结果清单。产物：
reports/u1-8823-20261104/WALKTHROUGH.json
"""
from __future__ import annotations

import io
import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8823"
REPO = Path("/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix")
INPUT = REPO / "reports/install-cleanroom-20261104-r10b/first-task/cleanroom_first.pptx"
OUT = REPO / "reports/u1-8823-20261104/WALKTHROUGH.json"

# 本会话代理转发不了任何回环/外网目标：显式空代理。
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

steps: list[dict] = []


def call(method: str, path: str, *, body=None, headers=None, raw=None,
         timeout: float = 60.0) -> dict:
    data = raw if raw is not None else (
        json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if body is not None and raw is None:
        req.add_header("Content-Type", "application/json")
    t0 = time.perf_counter()
    try:
        with opener.open(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            code = resp.status
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            payload = {"raw": str(e)}
    dt = time.perf_counter() - t0
    steps.append({"method": method, "path": path, "status": code,
                  "seconds": round(dt, 3)})
    return {"code": code, "payload": payload, "seconds": dt}


# 0. 身份守卫（2026-11-04 教训：8823 曾被遗留旧进程占用，走查打到旧代码上）。
#    目标端口必须回答出**安装实例**身份：kind=installed 且 build_id 与本次安装
#    载荷一致，否则立即 FAIL——绝不做"打到谁就算谁"的走查。
EXPECTED_BUILD = "5d89f681136a52555e4aafca6a429f1b"
meta0 = call("GET", "/api/meta")
inst0 = meta0["payload"].get("instance") or {}
if inst0.get("kind") != "installed" or inst0.get("build_id") != EXPECTED_BUILD:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "verdict": "FAIL", "at": "identity_guard",
        "detail": "8823 未回答出预期的安装实例身份；拒绝在错误目标上走查。",
        "expected": {"kind": "installed", "build_id": EXPECTED_BUILD},
        "observed": inst0, "steps": steps,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"IDENTITY GUARD FAILED: kind={inst0.get('kind')} "
          f"build={inst0.get('build_id')}", flush=True)
    sys.exit(3)
print(f"identity guard OK: installed / {inst0.get('build_id')[:8]}", flush=True)

# 1. 首屏 bootstrap（安装实例首次调用 = 冷身份计算，记录耗时）
b = call("GET", "/api/bootstrap")
boot = b["payload"]
print(f"bootstrap: {b['code']} in {b['seconds']:.2f}s | "
      f"kind={boot.get('instance', {}).get('kind')} "
      f"build_id={boot.get('instance', {}).get('build_id')}", flush=True)

meta = call("GET", "/api/meta")
hist = call("GET", "/api/history")

# 2. 上传原稿
data = INPUT.read_bytes()
up = call("POST", "/api/upload", raw=data,
          headers={"X-Filename": "u1_fullchain.pptx",
                   "Content-Type": "application/octet-stream"})
up_path = up["payload"].get("path")
print(f"upload: {up['code']} -> {up_path}", flush=True)

# 3. 创建任务并启动 run（/api/make，默认主题 + 说明卡兜底默认开启）
theme = boot.get("theme_default") or "adaptive"
mk = call("POST", "/api/make",
          body={"source": up_path, "theme": theme}, timeout=120)
mk_payload = mk["payload"]
job_id = mk_payload.get("job_id") or (mk_payload.get("job") or {}).get("job_id")
print(f"make: {mk['code']} job_id={job_id}", flush=True)
if not job_id:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"verdict": "FAIL", "at": "make",
                               "steps": steps, "make": mk_payload},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    sys.exit(2)

# 4. 轮询 run（云链路可能 2-5 分钟）
run_done, run_last = False, None
for i in range(120):
    time.sleep(5)
    st = call("GET", f"/api/jobs/{job_id}")
    j = st["payload"]
    verdict = j.get("verdict") or (j.get("job") or {}).get("verdict")
    stage = j.get("stage") or (j.get("job") or {}).get("stage")
    run_last = {"poll": i, "verdict": verdict, "stage": stage}
    print(f"  poll[{i}] verdict={verdict} stage={stage}", flush=True)
    if verdict and verdict not in ("RUNNING", "QUEUED", "PENDING"):
        # 产品终态词汇：DEVELOPMENT_CANDIDATE = run 成功待验收；FAILED/CANCELLED = 失败。
        run_done = verdict in ("DEVELOPMENT_CANDIDATE", "SUCCESS", "OK", "PASS",
                               "COMPLETED")
        break

# 5. validate（异步），等本轮操作真正结束再导出
va = call("POST", f"/api/jobs/{job_id}/validate", body={})
val_done, val_last = False, None
for i in range(60):
    time.sleep(5)
    st = call("GET", f"/api/jobs/{job_id}")
    j = st["payload"]
    v = (j.get("validation") or {}).get("verdict") or j.get("validation_verdict")
    verdict = j.get("verdict") or (j.get("job") or {}).get("verdict")
    busy = j.get("op")
    # op 字段在操作结束后仍存在（含 state=done 与结果），只有 state=running 才算忙。
    busy_running = bool(busy) and (busy.get("state") == "running")
    val_last = {"poll": i, "validation_verdict": v, "verdict": verdict,
                "op_busy": busy_running,
                "op_state": (busy or {}).get("state")}
    print(f"  vpoll[{i}] validation={v} verdict={verdict} "
          f"op_state={(busy or {}).get('state')}", flush=True)
    # 2026-11-04 修复：首轮走查在 validate 异步未收尾时就 POST export，
    # 被产品以 409 WB.OP_IN_PROGRESS 正确拒绝——那是走查脚本的时序错误，
    # 不是产品缺陷。这里等 validation 出结果**且**没有进行中操作再继续。
    if v and not busy_running:
        val_done = v in ("EXECUTED", "PASSED", "DEVELOPMENT_CANDIDATE")
        break

# 6. export（对 409 OP_IN_PROGRESS 做有界重试）
ex = None
for attempt in range(4):
    ex = call("POST", f"/api/jobs/{job_id}/export", body={})
    err = (ex["payload"] or {}).get("error") or {}
    if ex["payload"].get("exported"):
        break
    if ex["code"] == 409 or err.get("code") == "WB.OP_IN_PROGRESS":
        print(f"  export 尝试 {attempt + 1} 遇 OP_IN_PROGRESS，5s 后重试", flush=True)
        time.sleep(5)
        continue
    break
print(f"export: {ex['code']} exported={ex['payload'].get('exported')}", flush=True)

res = call("GET", f"/api/jobs/{job_id}/result")

ok = bool(run_done and val_done and ex["payload"].get("exported"))
report = {
    "schema": "wengao.u1-8823.walkthrough.v1",
    "scope": "安装实例（build-5d89f681, 安装 根 U, port 8823）UI HTTP 全链复验；"
             "Cua 点击层与原生实播录像另行登记",
    "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "bootstrap_seconds": round(b["seconds"], 2),
    "bootstrap_instance": boot.get("instance"),
    "bootstrap_vision": boot.get("vision"),
    "theme_default": boot.get("theme_default"),
    "theme_registry_version": boot.get("theme_registry_version"),
    "job_id": job_id,
    "theme": theme,
    "run_final": run_last,
    "validate_final": val_last,
    "export_result": {k: ex["payload"].get(k) for k in ("exported", "gate", "detail")},
    "result_index": res["payload"] if res["code"] == 200 else {"code": res["code"]},
    "steps": steps,
    "verdict": "PASS" if ok else "FAIL",
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"verdict: {report['verdict']} -> {OUT}", flush=True)
sys.exit(0 if ok else 1)
