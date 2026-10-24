"""事故台账与"当前交付资格"。

事故 incident-20261017-f88b 里，已经被确认不可读的结果仍然可以：
- 在工作室显示为"真实合格候选"；
- 通过 `/api/jobs/<id>/result` 列出并下载（导出清单里的哈希与文件一致，所以
  下载绑定"校验通过"——但那份文件本身是坏的）。

结论：**哈希一致只证明"文件没被换过"，不证明"结果合格"**。因此需要一层
与事故/状态绑定的资格判断，供接纳、导出、固定下载共同使用：

    资格 = 当前 attempt 的验收记录 + 候选哈希一致 + 判定允许 + 未被事故失效

本模块只做**读取与登记**，不改写历史报告、不删除文件、不伪装 PASS。
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
LEDGER_NAME = "incident_ledger.json"
ALLOWED_VERDICTS = ("DEVELOPMENT_CANDIDATE",)


def now_iso() -> str:
    return datetime.now(CST).isoformat()


def sha256_file(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ledger_path(root) -> Path:
    """事故台账位置：数据根下 `incidents/`。

    环境变量 `WENGAO_INCIDENT_LEDGER` 可显式指定（用于把隔离实例指向
    同一份只读台账做验证），但产品路径不依赖它。
    """
    env = os.environ.get("WENGAO_INCIDENT_LEDGER")
    if env:
        return Path(env)
    return Path(root) / "incidents" / LEDGER_NAME


def load(root) -> dict:
    p = ledger_path(root)
    if not p.is_file():
        return {"schema_version": "1.0.0", "incidents": [], "path": str(p)}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            raise ValueError("not a dict")
        d.setdefault("incidents", [])
        d["path"] = str(p)
        return d
    except Exception as e:  # noqa: BLE001
        # 台账损坏不等于"没有事故"：按"全部阻断"处理，并如实带出原因。
        return {"schema_version": "1.0.0", "incidents": [], "path": str(p),
                "corrupt": f"{type(e).__name__}: {e}"}


def append_entry(root, entry: dict) -> dict:
    """追加一条事故登记（append-only，原子写）。"""
    p = ledger_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    d = load(root)
    rec = dict(entry)
    rec.setdefault("incident_id", "inc-" + uuid.uuid4().hex[:10])
    rec.setdefault("created_at", now_iso())
    rec.setdefault("schema_version", "1.0.0")
    d["incidents"].append(rec)
    d.pop("corrupt", None)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return rec


def blocking_for(root, job_id=None, sha256s=None) -> list[dict]:
    """返回与给定任务/文件哈希相关的**当前生效**事故。

    匹配规则：
    - 登记了 `job_ids` 且包含该 job → 命中；
    - 登记了 `artifact_sha256` 且与给定哈希相同 → 命中；
    - 登记了 `scope="all"`（例如"该数据根的渲染环境已确认不可信"）→ 命中。
    已 `resolved` 的登记不命中。
    """
    d = load(root)
    out = []
    if d.get("corrupt"):
        out.append({"incident_id": "ledger-corrupt", "reason":
                    f"事故台账损坏，无法确认结果是否受影响：{d['corrupt']}"})
        return out
    shas = {s for s in (sha256s or []) if s}
    for rec in d.get("incidents", []):
        if rec.get("resolved"):
            continue
        if rec.get("scope") == "all":
            out.append(rec)
            continue
        if job_id and job_id in (rec.get("job_ids") or []):
            out.append(rec)
            continue
        hits = shas & set(rec.get("artifact_sha256") or [])
        if hits:
            r = dict(rec)
            r["matched_sha256"] = sorted(hits)
            out.append(r)
    return out


def delivery_state(job_dir, root) -> dict:
    """当前交付资格（接纳/导出/固定下载共用）。

    只有 `allowed=True` 才能走普通成功出口。任何一项取不到证据都算不允许——
    "读不出来"不能当"没问题"。
    """
    job_dir = Path(job_dir)
    job_p = job_dir / "job.json"
    if not job_p.is_file():
        return {"allowed": False, "code": "WB.JOB_NOT_FOUND", "reasons": ["任务不存在"]}
    job = json.loads(job_p.read_text(encoding="utf-8"))
    vp = job_dir / "validation.json"
    reasons: list[dict] = []
    v = None
    if vp.is_file():
        try:
            v = json.loads(vp.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            reasons.append({"code": "EVIDENCE.UNREADABLE", "detail": str(e)})
    else:
        reasons.append({"code": "GATE.NOT_VALIDATED", "detail": "尚无验收记录"})

    stem = Path(job.get("input_path") or "").stem
    cand = job_dir / "candidate" / f"{stem}.themed.pptx"
    cand_sha = None
    if cand.is_file():
        cand_sha = sha256_file(cand)
    else:
        reasons.append({"code": "GATE.ARTIFACT_MISSING", "detail": "当前候选不存在"})

    # "RUNNING" 只有在**当前 attempt 还没有验收记录**时才代表"结果尚未产生"。
    # 新尝试开始时会先归档上一轮的 validation，因此这里不会把旧成功当成新结果。
    current_validation = bool(v and v.get("attempt_id") == job.get("attempt_id"))
    if job.get("verdict") in ("RUNNING", "QUEUED") and not current_validation:
        reasons.append({"code": "GATE.IN_PROGRESS",
                        "detail": f"当前尝试仍在 {job.get('verdict')} 且尚无本轮验收；"
                                  "本轮结果尚未产生"})
    if job.get("verdict") == "CANCELLED":
        reasons.append({"code": "GATE.LATE_RESULT_REJECTED", "detail": "当前 attempt 已取消"})
    if v:
        if v.get("attempt_id") != job.get("attempt_id"):
            reasons.append({"code": "EVIDENCE.STALE",
                            "detail": f"验收记录绑定 attempt {v.get('attempt_id')}，"
                                      f"当前 {job.get('attempt_id')}：旧判定不能给新尝试背书"})
        if cand_sha and v.get("candidate_pptx_sha256") and cand_sha != v["candidate_pptx_sha256"]:
            reasons.append({"code": "EVIDENCE.STALE",
                            "detail": "候选文件与验收记录登记的哈希不一致"})
        if v.get("verdict") not in ALLOWED_VERDICTS:
            reasons.append({"code": "GATE.RELEASE_BLOCKED",
                            "detail": f"判定为 {v.get('verdict')}，不是可交付候选"})

    inc = blocking_for(root, job_id=job.get("job_id"), sha256s=[cand_sha, job.get("input_hash")])
    for rec in inc:
        reasons.append({"code": "INCIDENT.BLOCKED",
                        "detail": f"事故 {rec.get('incident_id')}：{rec.get('reason')}",
                        "incident_id": rec.get("incident_id")})

    allowed = not reasons
    return {"allowed": allowed, "code": reasons[0]["code"] if reasons else None,
            "reasons": reasons, "candidate_sha256": cand_sha,
            "attempt_id": job.get("attempt_id"), "verdict": (v or {}).get("verdict"),
            "job_verdict": job.get("verdict"),
            "incidents": [r.get("incident_id") for r in inc],
            "export_path": job.get("export_path")}
