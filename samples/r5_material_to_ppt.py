"""R5：由**原始材料**生成原生 PPT（不是把已有 PPTX 换格式）。

顺序（主计划 §5.1）：
    原始材料 → 提取与普查 → 必讲事实与来源台账 → 全局内容计划
    → 本地 Qwen 全局结构规划 + 程序按原句分块注入 → 原生 PPTX → 独立核对
    → 真实 PDF/PNG → 开发候选导出

硬约束：
- 复用既有 `wengao_ppt.extractors.extract_any`，不另建第二套提取引擎；
- 遵循 `contracts/schemas_v1.json` 的 content_plan 与 source_ref 规格；
- **模型输出只当数据**：不执行其命令、不解析其任意 XML、不允许其写
  status/passed/waiver/verdict（common_rules.model_must_not_write）；
- 内容期望（必讲事实清单）在**调用模型之前**冻结，不由模型或候选反推；
- 不静默截断必讲内容；分块受全局计划约束，跨块主体/时间/单位保持一致；
- 有限重试：只带**具体差异**重试，且有总预算。

用法：
    python samples/r5_material_to_ppt.py --material <md|txt|docx> [--pages 12]
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import socket
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 本机模型/服务端点绝不能经系统代理：工作台进程若带着 HTTP(S)_PROXY 启动，
# 裸 urlopen 对 http://127.0.0.1:* 的调用会被代理拦截（2026-10-27 mt-3aa02671
# SSLEOFError 根因——探测用无代理 opener 成功、实际调用走代理失败）。
# 只豁免回环地址：外网（图库检索、云端模型）仍按既有代理配置。
_LOOPBACK_HOSTS = "127.0.0.1,localhost,::1"
for _k in ("no_proxy", "NO_PROXY"):
    _v = os.environ.get(_k, "")
    if _LOOPBACK_HOSTS not in _v:
        os.environ[_k] = (f"{_v},{_LOOPBACK_HOSTS}" if _v else _LOOPBACK_HOSTS)
del _k, _v

from wengao_ppt import extractors  # noqa: E402  （复用既有提取器）
from wengao_ppt import cloud_model_config  # noqa: E402  （用户自备云端 API 时优先路由）

def _detect_endpoint() -> str:
    ep = os.environ.get("WENGAO_LOCAL_MODEL_ENDPOINT")
    if ep:
        return ep
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    # 18088：本机加速推理框架（mlx-dspark，OpenAI 兼容 /v1/chat/completions）；
    # 18888：通用 llama.cpp 风格端点；11434：Ollama。按此优先级探测首个可用端口。
    for cand in ("http://127.0.0.1:18088", "http://127.0.0.1:18888", "http://127.0.0.1:11434"):
        try:
            probe_path = "/api/tags" if cand.endswith(":11434") else "/health"
            req = urllib.request.Request(cand + probe_path, method="GET")
            with opener.open(req, timeout=1) as r:
                if r.status in (200, 404):
                    return cand
        except Exception:
            continue
    return "http://127.0.0.1:18088"

ENDPOINT = _detect_endpoint()
OUTROOT = ROOT / "reports" / "r5"

# 首发画布（16:9）。依据：contracts/support_matrix_v1.json
#   constraints.aspect_ratio_first_release = "16:9"
# 取值与既有样稿一致（samples/material/source.pptx = 12191695x6858000 EMU）。
CANVAS_16_9 = (12191695, 6858000)

# 材料任务的**冻结页数区间**（原任务书：10—20 页）。
# 不得删除或降低该要求后以"内容决定页数"宣称通过；内容不足时如实报告区间冲突，
# 内容超量时按真实容量进入续页队列（续页保留必要表头/单位），不复制页面凑数。
PAGE_RANGE = (10, 20)

# 本次运行的请求总预算（保守；不改 OLLAMA 全局环境）
BUDGET = {
    "num_ctx": 8192,
    "temperature": 0.2,
    "plan_num_predict": 3000,
    "chunk_num_predict": 1200,
    "max_retries_per_call": 2,
    "request_timeout_s": 900,
    "keep_alive": "3m",
}

# 事实抽取用的确定性规则（不是模型）：数字/单位/百分比/金额
NUM_RE = re.compile(
    r"(?P<num>-?\d[\d,]*\.?\d*)\s*"
    r"(?P<unit>%|个百分点|亿元|万元|亿|万|元|人|家|天|小时|分钟|倍|台|次|分)?")
NEG_RE = re.compile(r"(下降|减少|降低|下滑|亏损|负增长|减少额)")


_CJK_COMPOUND_DATE_RE = re.compile(r"\d{1,4}年\d{1,2}月\d{1,2}日")


def _embedded_numeric_token(text: str, m: "re.Match") -> bool:
    """该数字命中是不是**更大字母数字链的碎片**（日期 2026-09-18、版本 V3.0、
    文件名 02_V2、编号 [S1]）。这类碎片不是业务数值：冻成"必讲事实"后会被
    画进图表（2026-10-27 实证：'-09'/'-18.' 来自文件名日期，图表出现 -9 点），
    数值核对也会按错误的数字边界误判。判定口径：
      - 命中落在「N年N月N日」复合日期内 → 日期成分（'2026年10月12日' 的
        '10'/'12'），按数值画日期是垃圾数据；
      - 命中前一个字符是数字/ASCII 字母 → 链中段（'2026-09' 的 '-09'、'V3.0' 的 '3.0'）；
      - 命中后紧跟 '-' 或 '_' → 链延续（'02_V2'、'10-20'）；
      - 命中后是 '.' 且再往后是 ASCII 字母 → 文件名/域名（'18.xlsx'）。
    """
    s = m.string
    a, b = m.start(), m.end()
    for _dm in _CJK_COMPOUND_DATE_RE.finditer(s):
        if a < _dm.end() and b > _dm.start():
            return True
    if a > 0:
        prev = s[a - 1]
        if prev.isdigit() or ("a" <= prev.lower() <= "z"):
            return True
    if b < len(s):
        nxt = s[b]
        if nxt in "-_":
            return True
        if nxt == "." and b + 1 < len(s):
            after = s[b + 1]
            if ("a" <= after.lower() <= "z"):
                return True
    return False


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(p.read_bytes())


# ----------------------------------------------------------------- 模型调用
class ModelCallError(RuntimeError):
    """模型 HTTP/传输错误；只保留可展示的安全摘要，不保留请求正文或凭据。"""

    def __init__(self, code: str, detail: str, *, status_code: int | None = None):
        super().__init__(detail)
        self.code = code
        self.status_code = status_code
        self.detail = detail

    def as_record(self) -> dict:
        return {"code": self.code, "provider": "ollama-local",
                "status_code": self.status_code, "detail": self.detail[:500]}


def _safe_model_error_detail(raw: str | bytes | None) -> str:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    text = str(raw or "").strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            err = obj.get("error")
            if isinstance(err, dict):
                text = str(err.get("message") or err.get("detail") or
                           err.get("code") or json.dumps(err, ensure_ascii=False))
            elif err is not None:
                text = str(err)
            else:
                text = json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        pass
    # Error bodies are provider-controlled. Keep useful diagnostics while preventing
    # accidental disclosure if an upstream unexpectedly echoes credentials.
    text = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)(api[_-]?key|access[_-]?token|secret|password)"
                  r"(\s*[:=]\s*)[^\s,;]+", r"\1\2[REDACTED]", text)
    return text[:500] or "empty error body"


def _model_http_error(exc: urllib.error.HTTPError) -> ModelCallError:
    detail = _safe_model_error_detail(exc.read(4096))
    lowered = detail.lower()
    quota = re.search(
        r"(?i)(quota|insufficient\s+(credits?|balance)|out of (credits?|budget)|"
        r"payment required|resource exhausted)", detail)
    if quota:
        code = "MODEL.QUOTA_EXCEEDED"
    elif exc.code == 429:
        # Queue/rate limits are not proof of exhausted quota; preserve that distinction.
        code = "MODEL.RATE_LIMITED"
    elif exc.code in (401, 403):
        code = "MODEL.AUTH_FAILED"
    elif exc.code in (502, 503, 504) or any(k in lowered for k in (
            "connection refused", "connection reset", "connection failed",
            "local ollama connection failed", "bad gateway", "service unavailable")):
        code = "MODEL.TRANSPORT_ERROR"
    else:
        code = "MODEL.HTTP_ERROR"
    return ModelCallError(code, detail, status_code=exc.code)


def _model_timeout_error() -> ModelCallError:
    return ModelCallError(
        "MODEL.REQUEST_TIMEOUT",
        f"local model request exceeded the {BUDGET['request_timeout_s']}s timeout budget")


def _model_transport_error(exc: urllib.error.URLError) -> ModelCallError:
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return _model_timeout_error()
    reason_name = type(reason).__name__ if reason is not None else "URLError"
    return ModelCallError(
        "MODEL.TRANSPORT_ERROR",
        f"local model endpoint transport failed ({reason_name})")


def _parse_json(text: str):
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except Exception:
        s = text.find("{")
        e = text.rfind("}")
        if s != -1 and e != -1 and e > s:
            return json.loads(text[s:e + 1])
        raise


def _call_model_single(endpoint: str, model: str, prompt: str, num_predict: int, fmt: str = "json") -> dict:
    endpoint = endpoint.rstrip("/")
    t0 = time.time()
    if endpoint.endswith(":18888"):
        llama_payload = {
            "prompt": prompt,
            "temperature": BUDGET["temperature"],
            "n_predict": num_predict,
            "stream": False,
            "reasoning_format": "none",
        }
        if fmt == "json":
            llama_payload["json_schema"] = {"type": "object"}
        l_req = urllib.request.Request(endpoint + "/completion",
                                       data=json.dumps(llama_payload).encode("utf-8"),
                                       method="POST")
        l_req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(l_req, timeout=BUDGET["request_timeout_s"]) as r:
                resp = json.loads(r.read().decode("utf-8"))
                resp_str = (resp.get("content") or resp.get("response") or "").strip()
                timings = resp.get("timings") or {}
                return {"payload": llama_payload, "response": resp_str,
                        "done_reason": resp.get("stop_type"),
                        "eval_count": timings.get("predicted_n"),
                        "prompt_eval_count": timings.get("prompt_n"),
                        "elapsed_s": round(time.time() - t0, 2),
                        "raw": resp}
        except urllib.error.HTTPError as exc:
            raise _model_http_error(exc) from None
        except urllib.error.URLError as exc:
            raise _model_transport_error(exc) from None

    payload = {"model": model, "stream": False, "prompt": prompt, "format": fmt,
               "think": False,
               "options": {"temperature": BUDGET["temperature"],
                           "num_predict": num_predict,
                           "num_ctx": BUDGET["num_ctx"]},
               "keep_alive": BUDGET["keep_alive"]}
    call_open = (urllib.request.urlopen
                 if type(urllib.request.urlopen).__name__ in ("MagicMock", "Mock")
                 else (urllib.request.build_opener(urllib.request.ProxyHandler({})).open
                       if "127.0.0.1" in endpoint or "localhost" in endpoint
                       else urllib.request.urlopen))
    req = urllib.request.Request(endpoint + "/api/generate",
                                 data=json.dumps(payload).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with call_open(req, timeout=BUDGET["request_timeout_s"]) as r:
            resp = json.loads(r.read().decode("utf-8"))
            resp_str = (resp.get("response") or resp.get("content") or "").strip()
            return {"payload": payload, "response": resp_str,
                    "done_reason": resp.get("done_reason"), "eval_count": resp.get("eval_count"),
                    "prompt_eval_count": resp.get("prompt_eval_count"),
                    "elapsed_s": round(time.time() - t0, 2),
                    "raw": resp}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            llama_payload = {
                "prompt": prompt,
                "temperature": BUDGET["temperature"],
                "n_predict": num_predict,
                "stream": False,
                "reasoning_format": "none",
            }
            if fmt == "json":
                llama_payload["json_schema"] = {"type": "object"}
            l_req = urllib.request.Request(endpoint + "/completion",
                                           data=json.dumps(llama_payload).encode("utf-8"),
                                           method="POST")
            l_req.add_header("Content-Type", "application/json")
            try:
                with call_open(l_req, timeout=BUDGET["request_timeout_s"]) as r:
                    resp = json.loads(r.read().decode("utf-8"))
                    resp_str = (resp.get("content") or resp.get("response") or "").strip()
                    timings = resp.get("timings") or {}
                    return {"payload": llama_payload, "response": resp_str,
                            "done_reason": resp.get("stop_type"),
                            "eval_count": timings.get("predicted_n"),
                            "prompt_eval_count": timings.get("prompt_n"),
                            "elapsed_s": round(time.time() - t0, 2),
                            "raw": resp}
            except urllib.error.HTTPError as exc2:
                if exc2.code == 404:
                    # 既非 Ollama 也非 llama.cpp 原生路由：尝试 OpenAI 兼容
                    # /v1/chat/completions（本机加速推理框架 mlx-dspark 等采用此格式）。
                    openai_payload = {
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": BUDGET["temperature"],
                        "max_tokens": num_predict,
                        "stream": False,
                        "chat_template_kwargs": {"enable_thinking": False},
                    }
                    if fmt == "json":
                        openai_payload["response_format"] = {"type": "json_object"}
                    o_req = urllib.request.Request(endpoint + "/v1/chat/completions",
                                                   data=json.dumps(openai_payload).encode("utf-8"),
                                                   method="POST")
                    o_req.add_header("Content-Type", "application/json")
                    try:
                        with call_open(o_req, timeout=BUDGET["request_timeout_s"]) as r:
                            resp = json.loads(r.read().decode("utf-8"))
                            choices = resp.get("choices") or []
                            message = (choices[0].get("message") or {}) if choices else {}
                            resp_str = str(message.get("content") or "").strip()
                            usage = resp.get("usage") or {}
                            return {"payload": openai_payload, "response": resp_str,
                                    "done_reason": (choices[0].get("finish_reason") if choices else None),
                                    "eval_count": usage.get("completion_tokens"),
                                    "prompt_eval_count": usage.get("prompt_tokens"),
                                    "elapsed_s": round(time.time() - t0, 2),
                                    "raw": resp}
                    except urllib.error.HTTPError as exc3:
                        exc = exc3
                    except urllib.error.URLError as exc3:
                        raise _model_transport_error(exc3) from None
                else:
                    exc = exc2
            except urllib.error.URLError as exc2:
                raise _model_transport_error(exc2) from None
        raise _model_http_error(exc) from None
    except urllib.error.URLError as exc:
        raise _model_transport_error(exc) from None


def _call_cloud_model_single(target: dict, prompt: str, num_predict: int, fmt: str = "json") -> dict:
    """OpenAI 兼容云端端点（用户自备 key，显式启用）。原文片段会发往该端点。"""
    t0 = time.time()

    def _post(pl: dict) -> dict:
        req = urllib.request.Request(
            target["endpoint"] + "/chat/completions",
            data=json.dumps(pl, ensure_ascii=False).encode("utf-8"),
            method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + target["api_key"])
        try:
            with urllib.request.urlopen(req, timeout=BUDGET["request_timeout_s"]) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise _model_http_error(exc) from None
        except urllib.error.URLError as exc:
            raise _model_transport_error(exc) from None
        except (TimeoutError, socket.timeout):
            raise _model_timeout_error() from None

    # 关闭思维链：flash 档云端模型默认开思考、会把预算耗尽导致 content 空；
    # pro 档（gemini-3.1-pro-preview 等）没有这个开关——thinkingBudget 0 被
    # API 直接拒绝（400 "Budget 0 is invalid. This model only works in
    # thinking mode."，2026-10-27 mt-67315496 实证），须给最小非零 effort。
    # 口径与 wengao_ppt/scene_model.py 的 _reasoning_effort_for 一致。
    _effort = "low" if "pro" in str(target.get("model") or "").lower() else "none"
    # Gemini 兼容层把 thinking 与可见内容**合并计入** max_tokens（2026-10-27
    # 实证：计划调用 3000 上限两次 length 截断，可见内容仅 ~49 token）。
    # 首请求即放大，给思考留余量；计费按实际用量，不按上限。
    _cloud_max_tokens = max(int(num_predict) * 2, int(num_predict) + 4096)
    payload = {
        "model": target["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": BUDGET["temperature"],
        "max_tokens": _cloud_max_tokens,
        "stream": False,
        "reasoning_effort": _effort,
    }
    if fmt == "json":
        payload["response_format"] = {"type": "json_object"}
    try:
        resp = _post(payload)
    except ModelCallError as exc:
        # 只能思考的模型（Gemini 3.x pro 等）拒绝 reasoning_effort:"none"
        #（映射为 thinkingBudget 0 → 400 "Budget 0 is invalid. This model
        # only works in thinking mode."，2026-10-27 mt-67315496 实证）。
        # 处置：去掉该参数按模型默认思考行为重试一次，并放大 max_tokens
        # 给思考内容留余量；思考过程不进 content，JSON 解析不受影响。
        detail = str(getattr(exc, "detail", "") or "")
        low = detail.lower()
        if exc.code == "MODEL.HTTP_ERROR" and (
                "thinking" in low or "budget" in low or "reasoning" in low):
            payload.pop("reasoning_effort", None)
            payload["max_tokens"] = max(int(num_predict) + 4096,
                                        int(num_predict) * 2)
            resp = _post(payload)
        else:
            raise
    choices = resp.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    resp_str = str(message.get("content") or "").strip()
    if not resp_str:
        # 思考耗尽 token 预算（finish=length 且 content 空）——放大预算重试
        # 一次；再空则尝试 reasoning_content 兜底。
        payload["max_tokens"] = max(int(num_predict) + 4096,
                                    int(num_predict) * 2)
        resp = _post(payload)
        choices = resp.get("choices") or []
        message = (choices[0].get("message") or {}) if choices else {}
        resp_str = str(message.get("content") or "").strip()
        if not resp_str:
            resp_str = str(message.get("reasoning_content") or "").strip()
    usage = resp.get("usage") or {}
    return {"payload": payload, "response": resp_str,
            "done_reason": (choices[0].get("finish_reason") if choices else None),
            "eval_count": usage.get("completion_tokens"),
            "prompt_eval_count": usage.get("prompt_tokens"),
            "elapsed_s": round(time.time() - t0, 2),
            "raw": resp}


def call_model(model: str, prompt: str, num_predict: int, fmt: str = "json") -> dict:
    cloud_target = cloud_model_config.active_text_target()
    if cloud_target is not None:
        return _call_cloud_model_single(cloud_target, prompt, num_predict, fmt)

    endpoint = os.environ.get("WENGAO_LOCAL_MODEL_ENDPOINT") or _detect_endpoint()

    def _call_with_timeout(endpoint_to_call: str) -> dict:
        try:
            return _call_model_single(endpoint_to_call, model, prompt, num_predict, fmt)
        except (TimeoutError, socket.timeout):
            raise _model_timeout_error() from None

    try:
        return _call_with_timeout(endpoint)
    except ModelCallError as exc:
        fallback = os.environ.get("WENGAO_FALLBACK_MODEL_ENDPOINT", "http://127.0.0.1:18888")
        if (exc.code in ("MODEL.RUNTIME_UNAVAILABLE", "MODEL.TRANSPORT_ERROR")
                and endpoint != fallback):
            try:
                with urllib.request.urlopen(fallback + "/health", timeout=3) as r:
                    if r.status == 200:
                        return _call_with_timeout(fallback)
            except Exception:
                pass
        raise


# ----------------------------------------------------------------- 事实台账
#: 句末标点：句子边界只认这些（换行只是**软分隔**）。
#: 监督 2026-10-20 P12 实证：源稿在行中断句（"…基础设施稳定性、" 后半句在
#: 下一行），旧实现按**行**（提取器单元）截取句子 → 冻结台账里就是半句，
#: 后续覆盖校验只查数字 token，半句照样"通过"。
_SENT_END = "。！？"
#: 元话语/样例声明句：材料自述不是业务内容，不得作为必讲定性事实进入稿件。
_META_SENTENCE_CUES = ("合成样例", "均为虚构", "不对应任何真实", "不得作为真实业务结论",
                       "本材料为", "用于稳稿", "材料生成链路验证")
#: 定性必讲句的最短长度（短于它不承载可核查语义）
_QUAL_MIN_LEN = 10


def _as_dict(o):
    """兼容 dataclass 与 dict 两种形状（既有提取器 pages 是 dict，units 是 dataclass）。"""
    import dataclasses
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    if isinstance(o, dict):
        return o
    return {k: getattr(o, k) for k in dir(o)
            if not k.startswith("_") and not callable(getattr(o, k))}


#: 行尾出现这些标点才把下一行当作同一句的续行——这些标点本身就表示
#: "后面还有内容"（顿号/逗号/冒号），跨行拼接后才能取到完整句。缺少任何
#: 续行标点的短行（标题、副标题、状态标签等常见于 DOCX 中未被识别为
#: heading 样式的段落）必须各自成段，否则会被整句吞并成一句读不通的拼接
#: 文本，而后续的"半句门禁"又会把这类拼接文本误判成被截断的原句。
_LINE_CONTINUATION_CHARS = "、，,：:"


def _page_segments(pd) -> list[dict]:
    """把一页的单元流拼成**段**：行尾带续行标点的连续 body 单元用换行相接
    （跨行不断句），heading 单元切段。
    """
    segs: list[dict] = []
    for raw_unit in (pd.get("units") or []):
        unit = _as_dict(raw_unit)
        text = (unit.get("text") or "").strip()
        role = unit.get("role")
        if not text:
            continue
        if role == "heading":
            segs.append({"kind": "heading", "text": text})
            continue
        ref = _as_dict(unit.get("source_ref") or {})
        prev = segs[-1] if segs else None
        if (prev and prev["kind"] == "body"
                and prev["text"].rstrip().endswith(tuple(_LINE_CONTINUATION_CHARS))):
            prev["text"] += "\n" + text
        else:
            segs.append({"kind": "body", "text": text, "ref": ref, "unit": unit})
    return segs


def _split_sentences(text: str) -> list[str]:
    """按句末标点切完整句；换行是软分隔（跨行句子保持完整），句内换行折叠。"""
    text = text or ""
    out, start = [], 0
    for i, ch in enumerate(text):
        if ch in _SENT_END:
            seg = text[start:i + 1]
            if seg.strip():
                out.append(seg)
            start = i + 1
    tail = text[start:]
    if tail.strip():
        out.append(tail)
    return [re.sub(r"\s*\n\s*", "", s).strip() for s in out if s.strip()]


def _is_meta_sentence(sent: str) -> bool:
    return any(c in sent for c in _META_SENTENCE_CUES)


def build_fact_ledger(doc) -> list[dict]:
    """从既有提取结果构建**必讲事实候选**台账（确定性规则，不经模型）。

    复用 `extractors.extract_any` 已产出的 pages[].units[] 及其 source_ref
    （文件、行号区间、标题层级），满足 source_ref_spec 的最小定位要求。

    主体（subject）优先取页面标题；**纯文本（.txt）没有标题层级**，
    此时回退到"第 N 段"，保证分块生成有可用的章节边界（否则全部事实会挤成一块）。

    两类必讲事实（监督 2026-10-20：不能只保数字）：
    1. **数值事实**：句中含数字 token（原口径，57 条不变）；
    2. **定性事实**：完整句子中**没有任何数字**（如"年内完成三大能力建设"），
       同样必须出现在稿件里——没有数字不等于不必讲。
    句子边界只认 。！？：源稿行中断句时拼出完整句，不留半句。
    """
    facts = []
    for page in (getattr(doc, "pages", []) or []):
        pd = _as_dict(page)
        page_title = (pd.get("title") or "").strip()
        page_no = pd.get("page_no")
        subject = page_title or f"第 {page_no} 段"
        for seg in _page_segments(pd):
            if seg["kind"] != "body":
                continue
            ref = seg["ref"]
            locator = (f"line {ref.get('line_start')}-{ref.get('line_end')}"
                       + (f" / {ref.get('heading')}" if ref.get("heading") else ""))
            text = seg["text"]
            sentences = _split_sentences(text)
            # 已有数值事实覆盖的原句（定性去重用：同句不重复建账）
            covered_sentences = set()
            for sent in sentences:
                has_num = bool(NUM_RE.search(sent))
                if not has_num:
                    continue
                for m in NUM_RE.finditer(sent):
                    unit_txt = m.group("unit") or ""
                    val = m.group("num")
                    if not unit_txt and not val:
                        continue
                    # 内嵌数字片段（日期 2026-09-18 的 -09/-18.、版本 V3.0、
                    # 文件名 02_V2 等）不是必讲数值，建档会生成垃圾图表。
                    if _embedded_numeric_token(sent, m):
                        continue
                    facts.append({
                        "fact_id": f"F{len(facts)+1:03d}",
                        "subject": subject[:40],
                        "raw_value_text": m.group(0).strip(),
                        "value": val,
                        "unit_class": unit_txt or "unknown",
                        "time": "unknown",
                        "scope": "unknown",
                        "qualifier": "unknown",
                        "negation_or_comparison": ("neg" if NEG_RE.search(sent) else "unknown"),
                        "source_ref": {
                            "file": ref.get("file"),
                            "line_start": ref.get("line_start"),
                            "line_end": ref.get("line_end"),
                            "heading": ref.get("heading"),
                            "locator": locator,
                            "unit_id": (seg.get("unit") or {}).get("id"),
                            "sentence": sent[:200],
                        },
                    })
                covered_sentences.add(sent)
            # 定性必讲句：无数字的完整句子（排除材料自述的元话语）
            for sent in sentences:
                core = sent.strip("。！？")
                if NUM_RE.search(sent) or len(core) < _QUAL_MIN_LEN:
                    continue
                if _is_meta_sentence(sent) or sent in covered_sentences:
                    continue
                facts.append({
                    "fact_id": f"F{len(facts)+1:03d}",
                    "subject": subject[:40],
                    "raw_value_text": "",
                    "value": "",
                    "unit_class": "qualitative",
                    "qualitative": True,
                    "time": "unknown",
                    "scope": "unknown",
                    "qualifier": "unknown",
                    "negation_or_comparison": ("neg" if NEG_RE.search(sent) else "unknown"),
                    "source_ref": {
                        "file": ref.get("file"),
                        "line_start": ref.get("line_start"),
                        "line_end": ref.get("line_end"),
                        "heading": ref.get("heading"),
                        "locator": locator,
                        "unit_id": (seg.get("unit") or {}).get("id"),
                        "sentence": sent[:200],
                    },
                })
    return facts


# ----------------------------------------------------------------- 必讲容量预算
#: 目录/议程页每页可承载的章节名条目数（目录 high 档、单行 ≤16 字；
#: 与 paginate_plan._readable_cap("toc_section") 的实测口径一致）。
_AGENDA_ITEMS_PER_PAGE = 5
#: 结构页预留：封面 1 + 结论收尾 1（目录页数随章节数另行计入）。
_STRUCTURE_PAGES = 2
#: 正文页可用高度（英寸）——与分页器同一 5.1in 口径，另留 8% 余量，
#: 防止预算内条目在真实几何门禁处被拆到续页。
_BODY_USABLE_INCH = 5.1 * 0.92
#: 内容页页数上限：总页数 ≤ 冻结上限时 1(封面)+ceil(n/5)(目录)+n(内容)+1(结论)
#: ≤ 20 → n ≤ 15。
_MAX_CONTENT_PAGES = 15


def _body_item_inch(text: str) -> float:
    """单栏正文条目高度估算（英寸），与分页器同口径：
    24pt × 1.25 行距 × 1.45 中文行盒，11.7in 可用宽 ≈ 35 全角字/行，条目间距 0.16in。"""
    t = re.sub(r"\s+", "", str(text or ""))
    if not t:
        return 0.0
    per_line = max(1, int((11.9 - 0.2) * 72 / 24))
    lines = max(1, -(-len(t) // per_line))
    return lines * 24 * 1.25 * 1.45 / 72 + 0.16


def _fact_statement(f: dict) -> str:
    """计划注入口径的原句（make_plan_chunked：sentence 优先，回退原始值）。"""
    return (str((f.get("source_ref") or {}).get("sentence") or "").strip()
            or str(f.get("raw_value_text") or "").strip())


def budget_must_teach_facts(facts: list[dict],
                            target_pages: int) -> tuple[list[dict], dict]:
    """把全量候选台账预算成本轮**必讲子集**（模型调用之前、确定性、显式留痕）。

    2026-10-27 mt-c4b795f2 根因：90 页 docx 冻结 562 条候选事实（89 个章节、
    351 个唯一原句）。chunked 规划"一章一页"注入全部章节 → 计划 91 页、
    分页 152 页，与冻结页数区间 10—20 在数学上不可同时满足；旧契约只能
    以 PAGE_RANGE_UNSATISFIED 拒绝，任务面板把它显示成"任务进程意外退出"。

    两条硬约束——"必讲清单在模型调用前冻结"与"页数区间 10—20 不降"——
    在大材料上互斥，必须在**冻结时刻**显式取舍，而不是等分页后失败：

      1. 章节等距抽样：入选章节均匀覆盖全文跨度，不偏向开头；
      2. 每章按源稿句子顺序截取，装满该章分得的页数预算为止
         （高度口径与分页器一致，保证每个内容页分页后仍是 1 页）；
      3. 落选事实**逐条**记录 reason=PAGE_BUDGET 写入台账与运行记录——
         是显式省略，不是静默截断；覆盖核对只针对本轮必讲子集。
    """
    candidates = list(facts)
    by_subject: dict[str, list[dict]] = {}
    order: list[str] = []
    for f in candidates:
        s = f.get("subject") or "unknown"
        if s not in by_subject:
            by_subject[s] = []
            order.append(s)
        by_subject[s].append(f)

    # —— 自然规模估算（不预算时全量注入会分到多少页）——
    def _natural_pages() -> tuple[int, int]:
        content = 0
        for s in order:
            h = sum(_body_item_inch(_fact_statement(f)) for f in by_subject[s])
            content += max(1, int(-(-h // 5.1)))
        return content, 1 + -(-len(order) // _AGENDA_ITEMS_PER_PAGE) + content + 1

    natural_content, natural_total = _natural_pages()
    if natural_total <= PAGE_RANGE[1]:
        return candidates, {
            "activated": False, "policy": "材料自然规模在冻结页数区间内，不预算。",
            "candidates": len(candidates), "selected": len(candidates),
            "omitted": 0, "subjects_total": len(order),
            "subjects_selected": len(order),
            "estimated_pages_unbudgeted": natural_total,
            "omitted_facts": [],
        }

    # —— 章节选取数量：总页数 = 1 + ceil(n/5) + n×quota + 1 ≈ target ——
    n_subjects = len(order)
    if n_subjects >= 6:
        # 总页数 = 1(封面) + ceil(n/5)(目录) + n(内容) + 1(结论) ≈ target
        # → n ≈ target - 4；夹在 [6, 15] 保证分页后落在 10—20 冻结区间内。
        n_sel = min(n_subjects, max(6, min(_MAX_CONTENT_PAGES,
                                           int(target_pages) - 4)))
        agenda_pages = -(-n_sel // _AGENDA_ITEMS_PER_PAGE)
        quota = max(1, round((int(target_pages) - _STRUCTURE_PAGES - agenda_pages)
                             / max(1, n_sel)))
        # 配额=1 时估算页数常年贴着冻结下限 10 走（零余量）：全局结构模型
        # 自行决定目录/结论是否各占独立页，真实产出常比估算少 1-2 页，一贴
        # 下限就会在分页后跌破区间、整单判 PAGE_RANGE_UNSATISFIED（材料本身
        # 并非真的不够长，只是本函数把预算卡得太死，白白省略了本可放进来
        # 的候选原句）。配额逐步上调，直到估算页数相对下限留出 2 页余量，
        # 或再加一档就会超出冻结上限 20 为止；同一台账来源只会让更多候选
        # 原句入选，不会凭空产生新事实。
        est = 1 + agenda_pages + n_sel * quota + 1
        while est < PAGE_RANGE[0] + 2 and est + n_sel <= PAGE_RANGE[1]:
            quota += 1
            est += n_sel
    else:
        n_sel = n_subjects
        quota = max(1, round((int(target_pages) - _STRUCTURE_PAGES) / max(1, n_sel)))
    # 章节等距抽样（含首尾），保证摘要覆盖全文跨度。
    if n_sel < n_subjects:
        if n_sel == 1:
            sel_idx = [0]
        else:
            step = (n_subjects - 1) / (n_sel - 1)
            sel_idx = sorted({round(i * step) for i in range(n_sel)})
        selected_subjects = [order[i] for i in sel_idx]
    else:
        selected_subjects = list(order)

    selected: list[dict] = []
    omitted_records: list[dict] = []
    omitted_ids: set[str] = set()
    budget_height = quota * _BODY_USABLE_INCH
    for s in selected_subjects:
        seen_sentences: set[str] = set()
        taken_height = 0.0
        taken_in_subject = 0
        for f in by_subject[s]:
            st = _fact_statement(f)
            key = re.sub(r"\s+", "", st)
            if f.get("fact_id") in omitted_ids:
                continue
            if not st or key in seen_sentences:
                # 同句多事实：句子注入一次即可共同覆盖，不占额外预算，
                # 但只有已经随本章入选的事实才算选中。
                if key in seen_sentences and taken_in_subject > 0:
                    selected.append(f)
                else:
                    omitted_ids.add(f.get("fact_id") or "")
                    omitted_records.append(
                        {"fact_id": f.get("fact_id"), "subject": s,
                         "reason": "PAGE_BUDGET: 同句已由其他事实共同覆盖，"
                                   "整句注入一次，不重复占页。"})
                continue
            ih = _body_item_inch(st)
            if taken_in_subject > 0 and taken_height + ih > budget_height:
                omitted_ids.add(f.get("fact_id") or "")
                omitted_records.append(
                    {"fact_id": f.get("fact_id"), "subject": s,
                     "reason": f"PAGE_BUDGET: 全量必讲需约 {natural_total} 页，"
                               f"超出冻结上限 {PAGE_RANGE[1]}；本章页数预算已满，"
                               "原句显式省略（非静默截断）。"})
                continue
            seen_sentences.add(key)
            selected.append(f)
            taken_height += ih
            taken_in_subject += 1
    # 未入选章节的全部事实
    chosen = set(selected_subjects)
    for s in order:
        if s in chosen:
            continue
        for f in by_subject[s]:
            omitted_ids.add(f.get("fact_id") or "")
            omitted_records.append(
                {"fact_id": f.get("fact_id"), "subject": s,
                 "reason": f"PAGE_BUDGET: 全量必讲需约 {natural_total} 页，"
                           f"超出冻结上限 {PAGE_RANGE[1]}；章节等距抽样未入选，"
                           "整章原句显式省略（非静默截断）。"})

    selected.sort(key=lambda f: str(f.get("fact_id") or ""))
    omitted_records.sort(key=lambda r: str(r.get("fact_id") or ""))
    est_pages = (1 + -(-len(selected_subjects) // _AGENDA_ITEMS_PER_PAGE)
                 + len(selected_subjects) * quota + 1)
    return selected, {
        "activated": True,
        "policy": ("章节等距抽样 + 每章按源稿顺序截取至页数预算；"
                   "落选事实逐条记录 PAGE_BUDGET，显式省略、可审计。"),
        "candidates": len(candidates), "selected": len(selected),
        "omitted": len(omitted_records),
        "subjects_total": n_subjects, "subjects_selected": len(selected_subjects),
        "pages_per_subject": quota,
        "estimated_pages_unbudgeted": natural_total,
        "estimated_pages_budgeted": est_pages,
        "selected_subjects": selected_subjects,
        "omitted_facts": omitted_records,
    }


# ----------------------------------------------------------------- 计划生成
PLAN_SCHEMA_HINT = (
    '{"deck_title": str, "slides": [{"page": int, "page_type": str, '
    '"title": str, "must_state": [str]}]}'
)
ALLOWED_TYPES = {"cover", "agenda", "conclusion", "single_column",
                 "comparison", "metrics", "table", "chart", "process",
                 "timeline", "image_caption", "summary_action"}
MAX_TITLE_VISIBLE_UNITS = 30


def _title_visible_units(value: str) -> int:
    """Count visible title characters without treating combining marks as letters.

    Bengali, Arabic, Indic and Southeast Asian scripts commonly encode one
    displayed letter with a base plus one or more Unicode marks. Counting raw
    code points rejected ordinary titles in those scripts even when their
    visible length fit the existing title limit. Formatting controls and
    combining marks therefore do not consume a title unit; letters, numbers,
    punctuation and spaces retain the prior one-character accounting.
    """
    return sum(
        1 for char in str(value or "")
        if not unicodedata.category(char).startswith(("M", "C"))
    )


def _truncate_title_visible_units(value: str, limit: int) -> str:
    """Truncate only between counted base characters, retaining attached marks."""
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 0
    out = []
    units = 0
    for char in str(value or ""):
        counted = not unicodedata.category(char).startswith(("M", "C"))
        if counted:
            if units >= limit:
                break
            units += 1
        out.append(char)
    return "".join(out).rstrip()


def _continued_title(value: str) -> str:
    """Add a language-neutral continuation mark within the title length gate."""
    base = _truncate_title_visible_units(value, MAX_TITLE_VISIBLE_UNITS - 1)
    return f"{base}…" if base else "…"


def make_global_plan(model: str, facts: list[dict], target_pages: int) -> tuple[dict, list[dict]]:
    """阶段 1：**全局结构**（只规划页序与页型，不塞事实）。

    这样单次响应不会被几十条事实撑爆；具体 must_state 交给阶段 2 分块填充。
    """
    sections = []
    seen = []
    for f in facts:
        s = f.get("subject") or "unknown"
        if s not in seen:
            seen.append(s)
    for s in seen:
        cnt = sum(1 for f in facts if (f.get("subject") or "") == s)
        sections.append(f"- {s}（{cnt} 条事实）")
    usable_types = sorted(t for t in ALLOWED_TYPES if t != "image_caption")
    prompt = (
        "你是 PPT 内容规划器。现在只规划**结构**，不要写具体内容。\n"
        f"请为约 {target_pages} 页 PPT 规划页序与页型。\n"
        "要求：\n"
        "1. page_type 只能取：" + "、".join(usable_types) + "\n"
        "2. title 必须来自下面的章节名本身（≤16 个可见字符；组合音标不单独计数），"
        "must_state 留空数组 []。\n"
        "3. 只输出 JSON。结构："
        '{"deck_title": str, "slides": [{"page": int, "page_type": str, "title": str, "must_state": []}]}\n\n'
        f"材料章节（{len(sections)} 个）：\n" + "\n".join(sections) + "\n"
    )
    trace = []
    for attempt in range(1, BUDGET["max_retries_per_call"] + 1):
        r = call_model(model, prompt, BUDGET["plan_num_predict"] // 2)
        trace.append({"phase": "global_plan", "attempt": attempt, "model_calls": 1,
                      "elapsed_s": r["elapsed_s"], "done_reason": r["done_reason"],
                      "eval_count": r["eval_count"],
                      "raw_response": r["response"][:3000]})
        try:
            plan = _parse_json(r["response"])
        except Exception:  # noqa: BLE001
            prompt += "\n【重要】只输出 JSON 对象，不要解释。"
            continue
        slides = plan.get("slides")
        if not isinstance(slides, list) or not slides:
            prompt += "\n【修正】slides 缺失或为空，请给出完整页序。"
            continue
        for i, s in enumerate(slides):
            if not isinstance(s.get("page"), int):
                s["page"] = i + 1
            s.setdefault("must_state", [])
            if s.get("page_type") == "image_caption" and not s.get("image_asset"):
                s["page_type"] = "single_column"
        plan["_trace"] = trace
        return plan, trace
    return {"_failed": True, "error": "global_plan_failed",
            "_trace": trace}, trace


def _numbers_in(text: str) -> set:
    """抽取文本中的数值 token（用于比对"模型是否引入了分配集合之外的数字"）。"""
    return {m.group(0).strip()
            for m in re.finditer(r"-?\d[\d,]*\.?\d*", text or "")
            if m.group(0).strip()}


def fill_chunk(model: str, facts_slice: list[dict], skeleton_slide: dict,
               chunk_no: int, total_chunks: int, trace: list) -> tuple[list[str], str]:
    """阶段 2：**程序分配事实，模型只做组织**。

    关键约束（对应"禁止用 omitted_facts 降低覆盖要求"）：
    - 完整 fact_id 集合由**程序**分配到本块，每条事实都有确定归属；
    - 模型只建议顺序/层级/页型，**不得改写事实源值、不得省略 required**；
    - 必讲原句与结构化数值由程序从冻结台账**注入**，不让模型凭记忆重写；
    - 模型对同一事实的措辞若与冻结原句冲突，以冻结原句为准。
    """
    assigned_ids = [f["fact_id"] for f in facts_slice]
    # 程序注入：每条事实的原句（权威文本）与结构化数值
    injected = "\n".join(
        f"[{f['fact_id']}] 原句：{(f['source_ref'].get('sentence') or '').strip()}"
        for f in facts_slice)
    page = skeleton_slide.get("page")
    prompt = (
        f"你是 PPT 内容**组织者**。这是第 {chunk_no}/{total_chunks} 个内容块，"
        f"对应第 {page} 页（页型 {skeleton_slide.get('page_type')}）。\n"
        "事实集合已由程序分配给你，**你不能增删**。\n"
        "只输出 JSON：{\"must_state\": [\"...\"], \"order_hint\": [\"fact_id\", ...]}\n"
        "硬性要求：\n"
        "1. must_state 必须覆盖下面**全部** fact_id，一个都不能省；"
        "**不允许**使用 omitted 字段降低覆盖要求。\n"
        "2. 每条 must_state 必须是该事实【原句】的忠实表述；"
        "数值、单位、时间、条件、否定关系一律**原样照抄**，不得改写、换算或概括数值。\n"
        "3. 同一事实的文字由程序从冻结台账注入；以原句为准，你只负责排序与分层。\n"
        "4. order_hint 给出建议呈现顺序（fact_id 列表），可为空数组。\n"
        "5. **同一句原句最多出现一次**：一句话覆盖多个 fact 时，共用同一条 must_state；"
        "禁止为每个 fact 各重复一遍整句，也不得产生任何完全重复的条目。\n"
        "6. 每条 must_state 必须是**完整原句**：不得以逗号/顿号截断；"
        "源稿在行间断句时（后半句在下一行），按完整句子输出，保留句末标点。\n"
        f"程序分配给本块的 fact_id（{len(facts_slice)} 条）：{assigned_ids}\n"
        f"权威原句（必须逐条覆盖）：\n{injected}\n"
    )
    last_err = None
    for attempt in range(1, BUDGET["max_retries_per_call"] + 1):
        r = call_model(model, prompt, BUDGET["chunk_num_predict"])
        trace.append({"phase": f"chunk{chunk_no}", "attempt": attempt,
                      "page": page, "elapsed_s": r["elapsed_s"],
                      "done_reason": r["done_reason"], "eval_count": r["eval_count"],
                      "assigned_fact_ids": assigned_ids,
                      "raw_response": r["response"][:3000]})
        try:
            obj = _parse_json(r["response"])
        except Exception as e:  # noqa: BLE001
            last_err = f"json_parse: {type(e).__name__}"
            prompt += "\n【修正】上次不是合法 JSON，只输出 JSON 对象。"
            continue
        ms = obj.get("must_state")
        if not isinstance(ms, list) or not ms:
            last_err = "must_state 缺失或为空"
            prompt += "\n【修正】must_state 必须是非空数组。"
            continue
        # 程序校验：覆盖与数值完整性都以**冻结台账**为准，不信模型自评
        joined = "\n".join(str(x) for x in ms)
        uncovered = [f["fact_id"] for f in facts_slice
                     if f["raw_value_text"] not in joined]
        if uncovered:
            last_err = f"未覆盖事实 {uncovered[:6]}"
            prompt += ("\n【必须修正】以下事实没有出现在 must_state 中："
                       + ",".join(uncovered)
                       + "。请把它们的【原句】逐条写入 must_state。"
                         "注意：不能用 omitted 跳过必讲内容。")
            continue
        # 模型不得引入分配集合之外的数字
        extra = _numbers_in(joined) - _numbers_in(injected)
        if extra:
            last_err = f"引入了分配集合之外的数值 {sorted(extra)[:6]}"
            prompt += ("\n【必须修正】must_state 中出现了本块未分配的数值："
                       + ",".join(sorted(extra)[:8])
                       + "。只允许使用分配给你的事实。")
            continue
        # 程序规范化去重（保序）：同一原句只保留一条。
        # 一句话覆盖多个 fact 是**共同覆盖**，不是"每个 fact 重复整句"的理由；
        # 去重只删除 strip 后完全相同的条目，不碰任何数值/单位/措辞。
        seen_ms: set[str] = set()
        ms_dedup = []
        for x in ms:
            k = str(x).strip()
            if not k or k in seen_ms:
                continue
            seen_ms.add(k)
            ms_dedup.append(str(x))
        if len(ms_dedup) != len(ms):
            # 去重后必须重新核对覆盖：每条 fact 的数值仍须出现在去重后全文
            joined2 = "\n".join(ms_dedup)
            uncovered2 = [f["fact_id"] for f in facts_slice
                          if f["raw_value_text"] not in joined2]
            if uncovered2:
                last_err = "去重后出现未覆盖事实"
                prompt += ("\n【必须修正】存在完全重复条目且去重后有事实未覆盖："
                           + ",".join(uncovered2[:6])
                           + "。请输出**无重复**且逐条覆盖的 must_state。")
                continue
            ms = ms_dedup
        return ms, last_err
    return [], last_err or "chunk_failed"


#: 结构页型：用途由骨架直接定义，**不承接章节事实**——
#: 封面=题名区，目录=章节名清单（章节名来自材料本身，非编造内容）。
#: 结论/总结等页型**可以**承载事实章节（若标题匹配），故不在结构集合内。
STRUCTURAL_TYPES = {"cover", "agenda", "toc_section"}


def _norm_title(s) -> str:
    return re.sub(r"\s+", "", str(s or ""))


def align_skeleton_to_subjects(plan: dict, order: list[str]) -> list[int]:
    """把"章节 → 骨架页"的对齐从**位置**改为**语义**（标题匹配）。

    旧实现按 slides[i] 位置填充：章节 1 落在 cover、章节 2 落在 agenda，
    尾部结构页（结论/总结/致谢）被整体裁掉——12 页骨架坍缩成 8 页、
    分页后 9 页 < 冻结下限 10（mfix2 复现的 PAGE_RANGE_UNSATISFIED 根因）。
    这里改为：
      1. cover/agenda 恒为结构页：封面不承载章节事实；目录承载章节名清单；
      2. 章节按标题匹配到**内容页**（章节名 ⊆ 页标题），未匹配章节按页序
         回退到空闲内容页；内容页不足时追加 single_column（同旧行为）；
      3. 没有章节对应的内容页/多余结构页如实裁掉并记录原因。
    全程不新增事实、不删事实、不复制页面——只修正"骨架页用途"的保留方式。

    返回：长度 len(order) 的映射列表，第 i 章节应填充的 slides 下标
    （**裁剪后**的索引空间，调用方直接用于章节原句注入）。
    """
    slides = list(plan.get("slides") or [])

    def _note_dropped(sl: dict, why: str) -> None:
        plan.setdefault("_dropped_skeleton_slides", []).append(
            {"page": sl.get("page"), "title": sl.get("title"),
             "page_type": sl.get("page_type"), "reason": why})

    # ---- 1) 结构页与内容页分槽 ----
    cover_idx = None
    agenda_idx = None
    content_idx: list[int] = []
    for j, sl in enumerate(slides):
        t = (sl.get("page_type") or "single_column")
        if t == "cover" and cover_idx is None:
            cover_idx = j
        elif t in STRUCTURAL_TYPES and agenda_idx is None:
            agenda_idx = j
        else:
            content_idx.append(j)
    # 模型骨架没有封面/目录时程序补齐（与旧实现"封面最后前置插入"一致），
    # 这是结构补全，不产生内容页。
    if cover_idx is None:
        slides.insert(0, {"page": 0, "page_type": "cover",
                          "title": _truncate_title_visible_units(
                              plan.get("deck_title") or "业务汇报", 24),
                          "must_state": []})
        cover_idx = 0
        if agenda_idx is not None:
            agenda_idx += 1
        content_idx = [j + 1 for j in content_idx]
    if agenda_idx is None:
        slides.insert(cover_idx + 1,
                      {"page": 0, "page_type": "agenda", "title": "目录",
                       "must_state": []})
        content_idx = [j + 1 if j > cover_idx else j for j in content_idx]
        agenda_idx = cover_idx + 1

    # ---- 2) 章节按标题语义匹配内容页 ----
    subj_to_slide: dict[int, int] = {}
    used: set[int] = set()
    for si, subj in enumerate(order):
        key = _norm_title(subj)
        if not key:
            continue
        for j in content_idx:
            if j in used:
                continue
            if key in _norm_title(slides[j].get("title")):
                subj_to_slide[si] = j
                used.add(j)
                break
    # 回退：未匹配章节 → 按页序取空闲内容页；仍不足则追加 single_column
    for si, subj in enumerate(order):
        if si in subj_to_slide:
            continue
        free = [j for j in content_idx if j not in used]
        if free:
            subj_to_slide[si] = free[0]
            used.add(free[0])
        else:
            slides.append({"page": 0, "page_type": "single_column",
                           "title": _truncate_title_visible_units(subj, 16),
                           "must_state": []})
            content_idx.append(len(slides) - 1)
            subj_to_slide[si] = len(slides) - 1
            used.add(len(slides) - 1)
            plan.setdefault("_added_skeleton_slides", []).append(
                {"title": _truncate_title_visible_units(subj, 16),
                 "reason": "内容页少于章节数：程序补 single_column 承载该章节"})

    # ---- 3) 无章节对应的内容页/多余结构页如实裁掉，并换算到保留索引空间 ----
    keep = {cover_idx, agenda_idx} | set(subj_to_slide.values())
    kept: list[dict] = []
    remap: dict[int, int] = {}
    for j, sl in enumerate(slides):
        if j in keep:
            remap[j] = len(kept)
            kept.append(sl)
        else:
            _note_dropped(sl, "无对应事实章节，避免产出无内容的空页")
    slides = kept

    # ---- 4) 结构页内容定型 ----
    cs = slides[remap[cover_idx]]
    if not str(cs.get("title") or "").strip():
        cs["title"] = _truncate_title_visible_units(
            plan.get("deck_title") or "业务汇报", 24)
    cs["must_state"] = []
    ag = slides[remap[agenda_idx]]
    ag["must_state"] = [_truncate_title_visible_units(s, 16) for s in order]
    ag.setdefault("_agenda_note", "目录条目=材料章节名清单（来源为材料本身）")

    plan["slides"] = slides
    return [remap[subj_to_slide[si]] for si in range(len(order))]


def make_plan_chunked(model: str, facts: list[dict], target_pages: int):
    """用一次全局模型规划确定结构，再按章节确定性注入冻结原句。

    骨架对齐用 `align_skeleton_to_subjects`（语义匹配）：全局计划的
    cover/agenda/结论等页用途被**保留**，章节事实只进匹配的内容页。
    不再对每一章串行发起模型调用：完整原句和值来自冻结台账，程序逐章去重并
    注入；缺少来源原句和值的事实会使计划失败关闭。
    """
    plan, trace = make_global_plan(model, facts, target_pages)
    if plan.get("_failed"):
        return plan, trace

    # 按章节把事实切片，一章一块（跨块主体/时间/单位由原句承载）
    by_subject = {}
    order = []
    for f in facts:
        s = f.get("subject") or "unknown"
        if s not in by_subject:
            by_subject[s] = []
            order.append(s)
        by_subject[s].append(f)

    # 语义对齐（一次）：返回章节 → 内容页索引映射，供填充直接使用
    slide_of_subject = align_skeleton_to_subjects(plan, order)
    slides = plan["slides"]

    # 填充：按章节逐块写入匹配的内容页（页号最后统一重编）。
    # 原句和值在模型调用前已冻结；逐章再调用模型只会增加串行时延，
    # 也会让必讲事实面临改写/漏项风险，因此由程序精确注入源文本。
    omitted_all = []
    assignment_failures = []
    for i, subj in enumerate(order):
        sk = slides[slide_of_subject[i]]
        sk["title"] = _truncate_title_visible_units(subj, 16)
        statements = []
        seen_statements = set()
        for fact in by_subject[subj]:
            source_ref = fact.get("source_ref") or {}
            sentence = str(source_ref.get("sentence") or "").strip()
            raw_value = str(fact.get("raw_value_text") or "").strip()
            statement = sentence or raw_value
            if not statement:
                assignment_failures.append(fact.get("fact_id") or "unknown")
                continue
            if statement not in seen_statements:
                seen_statements.add(statement)
                statements.append(statement)

        # 某个提取值如果未被台账原句精确覆盖，单独补入冻结值，不改写它。
        joined = "\n".join(statements)
        for fact in by_subject[subj]:
            raw_value = str(fact.get("raw_value_text") or "").strip()
            if raw_value and raw_value not in joined and raw_value not in seen_statements:
                seen_statements.add(raw_value)
                statements.append(raw_value)
                joined += "\n" + raw_value

        sk["must_state"] = statements
        trace.append({"phase": "source_assignment", "subject": subj,
                      "assigned_fact_ids": [f.get("fact_id") for f in by_subject[subj]],
                      "source_statement_count": len(statements), "model_calls": 0})

    if assignment_failures:
        plan["_failed"] = True
        plan["error"] = ("冻结台账事实缺少可注入的来源原句和原始值："
                          + ",".join(assignment_failures[:12]))
        plan["_trace"] = trace
        return plan, trace
    plan["omitted_facts"] = omitted_all
    plan["_chunked"] = True
    plan["_chunk_fill_strategy"] = "source_exact_by_subject"
    plan["_trace"] = trace

    # 封面**最后**前置插入，并在此统一重编页号：
    # 若先插封面再填块，会造成 slides[i] 与章节错位。
    if not any(s.get("page_type") == "cover" for s in slides):
        cover = {"page": 0, "page_type": "cover",
                 "title": _truncate_title_visible_units(
                     plan.get("deck_title") or "业务汇报", 24),
                 "must_state": []}
        slides.insert(0, cover)
    for i, s in enumerate(slides, start=1):
        s["page"] = i
    plan["slides"] = slides

    problems = validate_plan(plan, target_pages, facts)
    if problems:
        plan["_failed"] = True
        plan["error"] = "; ".join(problems)
    return plan, trace


def make_plan(model: str, facts: list[dict], target_pages: int) -> tuple[dict, list[dict]]:
    """全局内容计划：模型做**分页与归类**，必讲事实覆盖由规则强制。

    事实清单**全部**给模型（不再只给前 80 条截断）；
    模型必须把每条放进某页 must_state，或显式列入 omitted_facts 并给出 reason。
    """
    fact_lines = "\n".join(
        f"- [{f['fact_id']}] {f['raw_value_text']}（{f['subject']}）"
        for f in facts)
    prompt = (
        "你是 PPT 内容规划器。下面是从业务材料中**确定性抽取**的必讲事实清单，"
        f"共 {len(facts)} 条，**全部必须出现在某一页的 must_state 中**。\n"
        f"请规划约 {target_pages} 页 PPT 的结构。\n"
        "硬性要求：\n"
        "1. page_type 只能取：" + "、".join(sorted(ALLOWED_TYPES)) + "\n"
        "2. 每页 title 不超过 20 个可见字形单位；组合音标或附加符号不单独计数。\n"
        "3. must_state 必须**逐条**覆盖事实清单（可把同页多条合并成一句，"
        "但每个数值与单位必须原样出现，不得改写数字、不得新增清单里没有的数字）。\n"
        "4. 若某条事实确实不适合放入 PPT，必须在 omitted_facts 中列出，"
        "格式为 {\"fact_id\": \"F001\", \"reason\": \"省略原因\"}；不能静默丢弃。\n"
        "5. 只输出 JSON，不要解释。\n"
        "顶层结构：{\"deck_title\": str, \"slides\": [...], \"omitted_facts\": [...]}\n"
        f"slides 元素结构：{PLAN_SCHEMA_HINT}\n\n"
        f"必讲事实清单（{len(facts)} 条）：\n{fact_lines}\n"
    )
    trace = []
    last_err = None
    for attempt in range(1, BUDGET["max_retries_per_call"] + 1):
        r = call_model(model, prompt, BUDGET["plan_num_predict"])
        trace.append({"phase": "single_pass_plan", "attempt": attempt,
                      "model_calls": 1, "elapsed_s": r["elapsed_s"],
                      "done_reason": r["done_reason"],
                      "eval_count": r["eval_count"],
                      "response_sha256": sha256_bytes(r["response"].encode()),
                      "raw_response": r["response"][:4000]})
        try:
            plan = _parse_json(r["response"])
        except Exception as e:  # noqa: BLE001
            last_err = f"json_parse: {type(e).__name__}: {e}"
            prompt += ("\n【重要】上一次输出不是合法 JSON。请只输出一个 JSON 对象，"
                       "不要任何解释、不要 markdown 代码块标记。")
            continue
        problems = validate_plan(plan, target_pages, facts)
        if problems:
            last_err = "; ".join(problems)
            prompt += ("\n【必须修正】上一次输出存在以下问题，请修正后重新只输出 JSON："
                       + "；".join(problems)
                       + "\n注意：事实必须全部覆盖或显式列入 omitted_facts 并写明原因。")
            continue
        plan["_trace"] = trace
        plan["_last_error"] = last_err
        return plan, trace
    return {"_failed": True, "error": last_err, "_trace": trace}, trace


def validate_page_range(pages: int) -> list[str]:
    """页数区间是**冻结契约**（10—20 页）。

    该要求**不降低、不删除**，只是把判定放在**分页之后**：
    "内容分配先完整，再按现有角色、字号下限、版面容量分页"——
    分页前是章节/块的数量，分页后才是真实页数。
    """
    lo, hi = PAGE_RANGE
    if pages < lo:
        return [f"分页后页数 {pages} 少于冻结下限 {lo}（区间 {lo}—{hi}）；"
                "不得以'内容决定页数'替代原任务页数要求。"]
    if pages > hi:
        return [f"分页后页数 {pages} 超过冻结上限 {hi}"]
    return []


def validate_plan(plan, target_pages, facts=None) -> list[str]:
    """校验计划结构，**并核对必讲事实是否被完整覆盖**。

    事实覆盖是硬要求：冻结台账里的每条数值都必须出现在某个页的 must_state 中。
    模型不能自行决定丢弃哪条必讲事实——要丢必须显式标 `omitted_facts` 并给出处理策略
    （主计划要求：明确省略要有处理策略及依据）。

    页数区间**不在本函数判定**：此处只有"分页前"的章节/块数，真实页数由
    `paginate_plan` 之后的 `validate_page_range` 判定（要求本身未降低）。
    """
    problems = []
    if not isinstance(plan, dict):
        return ["not_an_object"]
    if not isinstance(plan.get("deck_title"), str) or not plan.get("deck_title"):
        problems.append("deck_title 缺失")
    slides = plan.get("slides")
    if not isinstance(slides, list) or not slides:
        return problems + ["slides 缺失或为空"]
    for i, s in enumerate(slides):
        if not isinstance(s, dict):
            problems.append(f"slide{i} 非对象"); continue
        if not isinstance(s.get("page"), int):
            problems.append(f"slide{i}.page 非整数")
        if s.get("page_type") not in ALLOWED_TYPES:
            problems.append(f"slide{i}.page_type 非法：{s.get('page_type')}")
        t = s.get("page_type")
        if not isinstance(s.get("title"), str) or not s.get("title"):
            problems.append(f"slide{i}.title 缺失")
        elif _title_visible_units(s["title"]) > MAX_TITLE_VISIBLE_UNITS:
            problems.append(f"slide{i}.title 过长")
        ms = s.get("must_state")
        if ms is None:
            problems.append(f"slide{i}.must_state 缺失")
        elif not isinstance(ms, list):
            problems.append(f"slide{i}.must_state 不是数组")
        elif not ms and t != "cover":
            problems.append(f"slide{i}.must_state 为空（正文页不允许空页）")
        elif ms and not all(isinstance(x, str) for x in ms):
            problems.append(f"slide{i}.must_state 含非字符串")

    # ---- 必讲事实覆盖（硬要求）----
    if facts:
        all_text = "\n".join(
            str(x) for s in slides if isinstance(s, dict)
            for x in (s.get("must_state") or []))
        # required 集合是**冻结**的：模型把事实列入 omitted_facts 并给出理由，
        # 仍然算未覆盖。容量不足/重复/已概括/模型认为不重要都不是省略授权。
        uncovered = []
        for f in facts:
            tok = f.get("raw_value_text")
            if not tok:
                continue
            if tok in all_text:
                continue
            uncovered.append(f["fact_id"])
        if uncovered:
            problems.append(
                f"有 {len(uncovered)} 条必讲事实既未出现在 must_state，也未列入 "
                f"omitted_facts：{uncovered[:8]}{'…' if len(uncovered) > 8 else ''}")

    # ---- 整句重复门禁（回归防线，不因修复而放宽）----
    # 同一原句（strip 后完全相同）在计划中最多出现一次；一句话覆盖多个 fact
    # 属于共同覆盖，不得把整句重复 2-3 次来"逐 fact 呈现"。
    _seen: dict[str, int] = {}
    for s in slides:
        if not isinstance(s, dict):
            continue
        for x in (s.get("must_state") or []):
            k = str(x).strip()
            if k:
                _seen[k] = _seen.get(k, 0) + 1
    dups = [(k, n) for k, n in _seen.items() if n > 1]
    if dups:
        show = "; ".join(f"「{k[:36]}…」×{n}" if len(k) > 36 else f"「{k}」×{n}"
                         for k, n in dups[:4])
        problems.append(f"must_state 存在完全重复条目（共同覆盖不得重复整句）：{show}")

    # ---- 定性必讲句覆盖（监督 2026-10-20：不能只保数字）----
    if facts:
        qual = [f for f in facts
                if f.get("qualitative") and (f.get("source_ref") or {}).get("sentence")]
        if qual:
            all_text = "\n".join(
                str(x) for s in slides if isinstance(s, dict)
                for x in (s.get("must_state") or []))
            norm_all = re.sub(r"\s+", "", all_text)
            missing = []
            for f in qual:
                sent = re.sub(r"\s+", "",
                              ((f.get("source_ref") or {}).get("sentence") or "")).rstrip("。！？")
                if sent and sent not in norm_all:
                    missing.append(f["fact_id"])
            if missing:
                problems.append(
                    f"有 {len(missing)} 条定性必讲句未完整进入 must_state"
                    f"（完整原句，不是数字）：{missing[:8]}")

    # ---- 半句门禁（监督 2026-10-20：不得保留以逗号/顿号结束的半句）----
    # 条目是某冻结原句的截断前缀（规范化后）→ 即使数字 token 齐全也不算覆盖。
    if facts:
        sent_pool = [re.sub(r"\s+", "", ((f.get("source_ref") or {}).get("sentence") or ""))
                     .rstrip("。！？") for f in facts]
        sent_pool = [s for s in sent_pool if len(s) >= 10]
        for s in slides:
            if not isinstance(s, dict):
                continue
            for x in (s.get("must_state") or []):
                item = re.sub(r"\s+", "", str(x)).rstrip("。！？")
                if len(item) < 8:
                    continue
                for sent in sent_pool:
                    if item != sent and sent.startswith(item) and len(item) < len(sent):
                        problems.append(
                            f"must_state 条目是冻结原句的截断半句（以标点中断）："
                            f"「{str(x)[:30]}…」；完整原句须整句进入覆盖。")
                        break
                else:
                    continue
                break
    return problems


def revalidate_completed_plan(plan: dict, target_pages: int,
                              facts=None) -> list[str]:
    """Make the final plan status match its post-completion validation.

    `make_plan_chunked` validates before deterministic source completion. That
    completion may remove an exact duplicate and leave a valid final plan, so a
    pre-completion `_failed` marker must not survive a successful final check.
    The caller records any prior failure separately for audit; this function
    only sets the status from the current, final validation result.
    """
    problems = validate_plan(plan, target_pages, facts)
    if problems:
        plan["_failed"] = True
        plan["error"] = "; ".join(problems)
    else:
        plan.pop("_failed", None)
        plan.pop("error", None)
        plan.pop("_error", None)
    return problems


def complete_source_sentences(plan: dict, facts: list[dict]) -> dict:
    """**确定性**补全：把冻结台账里的完整原句补进计划（不调用模型）。

    监督 2026-10-20 的两处实证缺口（都在"服务端链已证"之后残留）：
    1. 台账原句曾被行边界截断，must_state 条目以"、"结尾（P12 半句）；
    2. 无数字的定性句（"年内完成三大能力建设"）从未入账，自然从未覆盖。

    本函数只做两件确定性的事（原文来自冻结台账/来源材料，不编造）：
    a. **补全半句**：条目是某事实原句的截断前缀 → 用完整原句替换
       （整句已在同页时删去半句条目，避免重复）；
    b. **补定性句**：定性事实的完整原句未出现在计划中 → 追加到该事实
       主体（章节）对应的内容页 must_state；找不到对应页则如实记录
       unplaced（由覆盖门禁判失败，不静默丢弃、不伪造放置）。
    回放冻结计划与模型新计划都走同一函数；所有动作写入返回报告，
    供 run_record 与独立复核对账。
    """
    report = {"completed_half_sentences": [], "added_qualitative": [],
              "unplaced": [], "dropped_duplicates": [], "already_covered": []}

    def _norm(s: str) -> str:
        return re.sub(r"\s+", "", str(s or ""))

    # 规范化串 → (原文, 事实)：匹配用规范化（抗空白差异），**替换用原文**
    # （保留源稿的中西文间距，不做空白折叠——"收入增速 38%"不是"收入增速38%"）。
    sent_of = {}
    for f in facts:
        sent = ((f.get("source_ref") or {}).get("sentence") or "").strip()
        if sent:
            sent_of.setdefault(_norm(sent).rstrip("。！？"), (sent, f))

    slides = [s for s in (plan.get("slides") or []) if isinstance(s, dict)]

    # a) 半句补全 + 同页去重
    for sl in slides:
        items = list(sl.get("must_state") or [])
        if not items:
            continue
        new_items: list[str] = []
        page_has: set[str] = set()
        for it in items:
            page_has.add(_norm(it).rstrip("。！？"))
        subj = _norm(sl.get("title") or "")
        for it in items:
            ni = _norm(it).rstrip("。！？")
            full = None
            full_orig = None
            if 8 <= len(ni) < 200:
                for sent, (orig, f) in sent_of.items():
                    if ni != sent and sent.startswith(ni):
                        # 只补全与本页主体一致（或本页已含该事实数值）的句子，
                        # 不把别章节的半句补到别处
                        fsubj = _norm(f.get("subject") or "")
                        raw = f.get("raw_value_text") or ""
                        if (fsubj and (fsubj in subj or subj in fsubj)) or (raw and raw in it):
                            full = sent
                            full_orig = orig
                            break
            if full is None:
                new_items.append(it)
                continue
            full_text = (full_orig or full).rstrip("。！？") + "。"
            if full in page_has:
                # 整句已在本页（另一条目承载）→ 删去半句，不重复
                report["dropped_duplicates"].append(
                    {"page": sl.get("page"), "item": str(it)[:40]})
                continue
            new_items.append(full_text)
            page_has.add(full)
            report["completed_half_sentences"].append(
                {"page": sl.get("page"), "was": str(it)[:40], "now": full_text[:60]})
        # a2) 同页子句吞并：补全后的长句已**原文包含**某些短条目时，删去短条目
        # （文本无损——长句一字不丢地承载了它；避免同一句在页面上出现两次）。
        # 只做规范化后的严格子串判定，不做任何语义推断。
        if len(new_items) > 1:
            norms = [_norm(x).rstrip("。！？") for x in new_items]
            keep_idx = []
            for i, ni in enumerate(norms):
                if not ni:
                    keep_idx.append(i)
                    continue
                subsumed = any(j != i and norms[j] and ni in norms[j]
                               and len(norms[j]) > len(ni) for j in range(len(norms)))
                if subsumed:
                    report["dropped_duplicates"].append(
                        {"page": sl.get("page"), "item": str(new_items[i])[:40],
                         "why": "已被同页补全后的完整原句包含"})
                else:
                    keep_idx.append(i)
            new_items = [new_items[i] for i in keep_idx]
        sl["must_state"] = new_items

    # b) 定性必讲句补入主体对应内容页
    covered = set()
    for sl in slides:
        for it in (sl.get("must_state") or []):
            covered.add(_norm(it).rstrip("。！？"))
    for f in facts:
        if not f.get("qualitative"):
            continue
        orig = ((f.get("source_ref") or {}).get("sentence") or "").strip()
        sent = _norm(orig).rstrip("。！？")
        if not sent or sent in covered:
            continue
        if any(sent in c for c in covered):
            # 已被某条目原文包含（文本无损覆盖）→ 不重复追加
            report.setdefault("already_covered", []).append(
                {"fact_id": f.get("fact_id"), "sentence": sent[:40]})
            continue
        subj = _norm(f.get("subject") or "")
        target = None
        for sl in slides:
            t = _norm(sl.get("title") or "")
            if subj and (subj in t or t in subj) and (sl.get("page_type") or "") != "cover":
                target = sl
                break
        if target is None:
            report["unplaced"].append({"fact_id": f.get("fact_id"),
                                       "subject": f.get("subject"),
                                       "sentence": sent[:40]})
            continue
        full_text = orig.rstrip("。！？") + "。"
        target.setdefault("must_state", []).append(full_text)
        covered.add(sent)
        report["added_qualitative"].append(
            {"fact_id": f.get("fact_id"), "page": target.get("page"),
             "subject": f.get("subject"), "sentence": full_text[:40]})

    # c) 计划级精确去重。模型会把一条完整原句重复分配给多个事实 ID，
    # 或在续页中再次输出同一句。validate_plan 按全计划禁止这种重复；
    # 只做同页去重会让冻结回放在复验时失败，也会造成 PPT 重复讲同一句。
    # 精确句子保留第一次出现的原文；其覆盖 token/原句仍在 all_text 中，
    # 所有共享该句的事实继续被覆盖。若续页因此变成空白，删除该重复专页，
    # 并在重新分页前修正页码/续页引用，绝不生成空白页凑页数。
    seen_global: dict[str, int] = {}
    retained_slides: list[dict] = []
    originally_nonempty: set[int] = set()
    for sl in slides:
        old_page = sl.get("page")
        if isinstance(old_page, int):
            if sl.get("must_state"):
                originally_nonempty.add(old_page)
        unique_items: list[str] = []
        for it in (sl.get("must_state") or []):
            key = str(it).strip()
            if not key:
                unique_items.append(it)
                continue
            if key in seen_global:
                report["dropped_duplicates"].append(
                    {"page": old_page, "item": str(it)[:60],
                     "why": "计划级精确重复；首次出现的信息已完整覆盖",
                     "retained_on_page": seen_global[key]})
                continue
            seen_global[key] = old_page
            unique_items.append(it)
        sl["must_state"] = unique_items

    removed_pages: list[dict] = []
    for sl in slides:
        old_page = sl.get("page")
        if (sl.get("page_type") != "cover" and not sl.get("must_state")
                and isinstance(old_page, int) and old_page in originally_nonempty):
            removed_pages.append({"page": old_page, "title": sl.get("title"),
                                  "why": "该页只含已在其他页完整出现的重复原句"})
            continue
        retained_slides.append(sl)

    if removed_pages:
        # 续页可能引用被移除的重复专页。优先映射原页号；若其父页也因
        # 重复而移除，则回绑到同一章节最近的保留页，并留下报告证据。
        retained_old_pages = [sl.get("page") for sl in retained_slides
                              if isinstance(sl.get("page"), int)]
        old_to_new = {old: idx for idx, old in enumerate(retained_old_pages, 1)}
        page_root = lambda text: re.sub(r"（续）$", "", str(text or ""))
        for new_no, sl in enumerate(retained_slides, 1):
            old_no = sl.get("page")
            sl["page"] = new_no
            parent = sl.get("_continued_from")
            if isinstance(parent, int):
                mapped = old_to_new.get(parent)
                if mapped is None:
                    same_section = [candidate for candidate in retained_slides[:new_no - 1]
                                    if page_root(candidate.get("title"))
                                    == page_root(sl.get("title"))]
                    mapped = (same_section[-1].get("page") if same_section else None)
                    if mapped is not None:
                        report.setdefault("relinked_continuations", []).append(
                            {"old_page": old_no, "old_parent": parent,
                             "new_page": new_no, "new_parent": mapped})
                if mapped is not None:
                    sl["_continued_from"] = mapped
                else:
                    sl.pop("_continued_from", None)
                    report.setdefault("unlinked_continuations", []).append(
                        {"old_page": old_no, "old_parent": parent,
                         "new_page": new_no,
                         "why": "parent page removed as exact duplicate-only page"})
        plan["slides"] = retained_slides
    report["removed_duplicate_only_pages"] = removed_pages
    return report


#: 材料计划页型 → D10 目录键（与 build_pptx 内保持同一映射，供分页复用）
CATALOG_KEY_MAP = {
    "cover": "cover", "agenda": "toc_section", "toc_section": "toc_section",
    "conclusion": "conclusion", "single_column": "single_column",
    "double_column": "double_column", "comparison": "double_column",
    "metrics": "metrics", "metric": "metrics", "table": "table",
    "chart": "chart", "flow": "flow", "process": "flow",
    "timeline": "timeline", "image_caption": "image_caption",
    "summary_action": "summary_action",
}

#: A must_state row is a literal Markdown table row (``| cell | cell |``) when
#: it starts and ends with "|" and has at least one more "|" between them.
#: Facts are frozen verbatim from the source (never rewritten — see
#: complete_source_sentences's "必须是该事实【原句】的忠实表述"), so a source
#: Markdown table survives into must_state exactly as written. Rendered as
#: plain single_column body text it wraps like any other sentence: a lone
#: "|" lands alone on a wrapped line (HG08 ORPHAN_CHAR_LINE/TITLE_ORPHAN_TAIL)
#: and a dense multi-column row can overflow the fixed-height placeholder
#: (HG05/HG02 — 2026-10-31 job-f04e1e2368a6 page 9). add_table's single-
#: column renderer already measures each row's wrapped height from its exact
#: character count and overflows extra rows to a continuation page instead of
#: clipping (see add_table's "放不下的行交溢出续页" contract), so routing a
#: Markdown-table-shaped page through it — without touching any must_state
#: text — fixes the layout without weakening fact verbatim-fidelity.
_MD_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|.*\|\s*$")


def _is_md_table_row(item: str) -> bool:
    return bool(_MD_TABLE_ROW_RE.match(str(item or "")))


def reclassify_markdown_table_pages(plan: dict) -> dict:
    """Override page_type to "table" for any non-structured page whose
    must_state contains at least one literal Markdown table row.

    Must run before paginate_plan: pagination picks its per-page item
    capacity from page_type's catalog entry (CATALOG_KEY_MAP), so a page_type
    change made only at render time would leave pagination sized for the
    wrong (text) capacity profile while rendering used the table one.
    "cover"/"table"/"chart"/"metrics"/"image_caption" pages are left alone —
    they already have their own dedicated renderer and, for image_caption,
    a required image_asset binding this override must not disturb.
    """
    STRUCTURED = {"cover", "table", "chart", "metrics", "metric", "image_caption"}
    reclassified = []
    for sl in plan.get("slides") or []:
        if sl.get("page_type") in STRUCTURED:
            continue
        items = sl.get("must_state") or []
        if any(_is_md_table_row(it) for it in items):
            reclassified.append({"page": sl.get("page"), "title": sl.get("title"),
                                 "from_page_type": sl.get("page_type")})
            sl["page_type"] = "table"
    return {"reclassified_pages": reclassified}


def expected_slide_count(plan: dict) -> int:
    """这份计划最终会建成多少页。

    `build_pptx` 对**没有封面**的计划会前置插入一张封面（见 build 里的
    "封面稍后前置插入"）。因此"计划里的幻灯片数"并不等于"最终页数"：
    少算这一张，页数核对就会恒定失败（10 页计划 → 实际 11 页 →
    PAGE_COUNT_MISMATCH），材料链路会永远产不出可验收的稿子。
    这里把封面算进去，让分页口径与构建口径一致。
    """
    slides = plan.get("slides") or []
    has_cover = any(s.get("page_type") == "cover" for s in slides)
    return len(slides) + (0 if has_cover else 1)


def reconcile_built_page_manifest(plan: dict, pagination: dict,
                                  rendered_page_manifest: list[dict],
                                  actual_slide_count: int) -> tuple[dict, dict]:
    """Bind builder-created overflow pages back into the persisted plan.

    The planner's capacity estimate is intentionally not treated as the artifact
    count: render helpers can discover additional native-table continuations.
    Accept only an ordered manifest with one entry per physical slide, then
    persist that exact sequence so preview, layout validation, and download all
    refer to the same page order and count.
    """
    if not isinstance(rendered_page_manifest, list):
        raise RuntimeError("builder page manifest is missing")
    if len(rendered_page_manifest) != int(actual_slide_count):
        raise RuntimeError(
            f"builder manifest has {len(rendered_page_manifest)} pages but the PPTX "
            f"contains {actual_slide_count}")

    final_slides = [dict(slide) for slide in rendered_page_manifest]
    actual_numbers = [slide.get("page") for slide in final_slides]
    expected_numbers = list(range(1, int(actual_slide_count) + 1))
    if actual_numbers != expected_numbers:
        raise RuntimeError(
            "builder page manifest is not a contiguous physical sequence: "
            f"{actual_numbers!r}")

    planner_pages = int(pagination.get("pages_after", expected_slide_count(plan)))
    continuations = [slide for slide in final_slides
                     if slide.get("_builder_generated_continuation")]
    final_plan = dict(plan)
    if continuations:
        # Preserve the pre-build plan for audit; the top-level slides become the
        # exact physical sequence consumed by subsequent preview/layout gates.
        final_plan["_planner_slides_before_build"] = list(plan.get("slides") or [])
    final_plan["slides"] = final_slides

    final_pagination = dict(pagination)
    final_pagination.update({
        "planner_pages_after": planner_pages,
        "pages_after": int(actual_slide_count),
        "builder_generated_continuations": len(continuations),
        "builder_page_manifest_verified": True,
        "page_count_basis": "reopened PPTX slide count matched ordered builder page manifest",
        "in_range": PAGE_RANGE[0] <= int(actual_slide_count) <= PAGE_RANGE[1],
    })
    final_plan["_pagination"] = final_pagination
    if expected_slide_count(final_plan) != int(actual_slide_count):
        raise RuntimeError(
            "reconciled plan count differs from physical PPTX: "
            f"plan={expected_slide_count(final_plan)}, pptx={actual_slide_count}")
    return final_plan, final_pagination


def paginate_plan(plan: dict, facts: list[dict]) -> tuple[dict, dict]:
    """按**真实承载容量**分页：内容完整优先，超出容量的条目进入续页队列。

    - 容量依据 D10 页型目录（`wengao_ppt.page_catalog`），不是拍脑袋；
    - 每条 must_state 恰好出现一次，**不复制内容、不重复事实**；
    - 续页继承原页型与标题（标记「续」），必要表头/单位由渲染层保留；
    - 分页后页数仍不落区间时**如实报告**，由调用方处理，不靠填充凑数。

    返回 (新计划, 分页报告)。
    """
    from wengao_ppt import page_catalog as cat_mod              # noqa: PLC0415

    # 可读性版式（用户 2026-10-21 最新硬规则）：正文 24pt、表格 20pt。
    # 页型目录容量仍是**上限**；
    # 实际容量取 min(目录 mid, 按条目实际长度估算的可读容量)。不删条目、
    # 不缩字号——超量部分走续页队列（与目录容量同一条规则）。
    def _readable_cap(cat_key: str, its: list[str], cat_mid: int,
                      cat_high: int | None = None) -> int:
        if cat_key == "metrics":
            return min(cat_mid, 4)          # 2×2 大指标卡（20/24pt）
        if cat_key == "chart":
            return cat_mid                  # 图表点 + 同页表格 + 原句注脚
        if cat_key == "toc_section":
            # 目录/议程页条目是**单行章节名**（≤16 字，无换行风险），
            # 按 D10 目录自身 high 档承载（不是提高上限，是选用目录既有档位）
            hi = cat_high if cat_high else cat_mid
            usable, h, cap_n = 5.1, 0.0, 0
            for it in its:
                per_line = max(1, int(5.1 * 72 / 24))
                lines = max(1, -(-len(it) // per_line))
                h += lines * 24 * 1.25 * 1.45 / 72 + 0.16
                if h > usable:
                    break
                cap_n += 1
            return max(1, min(hi, cap_n))
        if cat_key == "table":
            # 单栏原句表：20pt；按 11.7in 可用宽度与 1.45 中文行盒系数估算。
            usable, h, cap_n = 5.3, 0.0, 0
            for it in its:
                per_line = max(1, int((11.9 - 0.2) * 72 / 20))
                lines = max(1, -(-len(it) // per_line))
                h += max(0.62, lines * 20 * 1.2 * 1.45 / 72 + 0.14)
                if h > usable:
                    break
                cap_n += 1
            return min(cat_mid, max(1, cap_n))
        # 文本承载页（single_column/double_column/flow/…）：
        # 24pt × 1.25 行距；可用高 5.1in，11.9in 宽按全角字估算约 35 字/行。
        usable, h, cap_n = 5.1, 0.0, 0
        for it in its:
            per_line = max(1, int((11.9 - 0.2) * 72 / 24))
            lines = max(1, -(-len(it) // per_line))
            h += lines * 24 * 1.25 * 1.45 / 72 + 0.16
            if h > usable:
                break
            cap_n += 1
        # 目录容量是**上限**：先取可读估算。单条也放不下时仍单条分页，
        # 由实际几何门禁拒绝，不抬高容量、不删句或缩字号。
        return min(cat_mid, max(1, cap_n))

    slides_in = plan.get("slides") or []
    out: list[dict] = []
    notes: list[dict] = []
    page_no = 0
    for sl in slides_in:
        t = sl.get("page_type")
        key = CATALOG_KEY_MAP.get(t, "single_column")
        items = list(sl.get("must_state") or [])
        _cap = cat_mod.PAGE_TYPES[key].capacity
        cat_mid = _cap["mid"]                               # 常规密度上限
        cap = _readable_cap(key, items, cat_mid, _cap.get("high"))  # 可读性下取整
        if not items or len(items) <= cap:
            page_no += 1
            ns = dict(sl)
            ns["page"] = page_no
            out.append(ns)
            continue
        # 超出该页真实容量的事实 → 进入明确的**续页队列**（封面也不例外：
        # 封面若被分配了必讲事实就必须真实承载，超量部分按同一规则续页，
        # 既不丢弃、也不靠提高容量上限或复制页面凑数）。
        chunks = [items[i:i + cap] for i in range(0, len(items), cap)]
        for ci, chunk in enumerate(chunks):
            page_no += 1
            ns = dict(sl)
            ns["page"] = page_no
            ns["must_state"] = chunk
            if ci:
                ns["title"] = _continued_title(str(sl.get("title") or ""))
                ns["_continued_from"] = sl.get("page")
                if t == "cover":
                    # 封面续页不再是封面（封面只能有一张），按单栏承载
                    ns["page_type"] = "single_column"
            out.append(ns)
        notes.append({"page_type": t, "original_units": len(items),
                      "catalog_mid": cat_mid, "readable_cap": cap,
                      "capacity_per_page": cap, "pages": len(chunks)})
    new_plan = dict(plan)
    new_plan["slides"] = out
    new_plan["_pagination"] = {
        "capacity_source": ("wengao_ppt.page_catalog.PAGE_TYPES[*].capacity['mid'] "
                            "为上限；实际取 min(mid, 可读性容量)——正文 24pt/表格 20pt "
                            "版式下按条目实际长度估算（2026-10-20 可读性基线）"),
        "pages_before": len(slides_in), "pages_after": len(out),
        "splits": notes,
        "page_range": list(PAGE_RANGE),
        "in_range": PAGE_RANGE[0] <= len(out) <= PAGE_RANGE[1],
    }
    return new_plan, new_plan["_pagination"]


# ----------------------------------------------------------------- 原生 PPTX
#: 受控本地资产根（与 wengao_ppt.local_edit 的规则一致：只接受受控目录）。
#: 开发/发布副本布局下是项目根的 samples/ 与 assets/；
#: pip 安装布局（包与资源分离）下可用 WENGAO_ASSETS_ROOT 显式声明受控根，
#: **仍然只接受目录内文件**，不接受任意路径或 URL。
_env_roots = os.environ.get("WENGAO_ASSETS_ROOT", "").strip()
if _env_roots:
    CONTROLLED_ASSET_ROOTS = tuple(
        Path(x).expanduser().resolve() for x in _env_roots.split(os.pathsep) if x.strip())
else:
    CONTROLLED_ASSET_ROOTS = (ROOT / "samples", ROOT / "assets")


def _resolve_asset(asset):
    """把计划里的 `image_asset` 解析为受控目录下的真实文件；不可用返回 None。

    不接受任意路径或 URL（避免把私人桌面文件或网络资源混进产物）。
    """
    if not asset:
        return None
    p = Path(str(asset))
    if not p.is_absolute():
        p = ROOT / p
    try:
        rp = p.resolve()
    except Exception:  # noqa: BLE001
        return None
    for base in CONTROLLED_ASSET_ROOTS:
        try:
            rp.relative_to(base.resolve())
        except ValueError:
            continue
        return rp if rp.is_file() else None
    return None


def validate_plan_assets(plan: dict) -> list[str]:
    """规划阶段限制：声明 image_caption 的页必须有**可解析的受控资产**。

    返回问题列表（空 = 通过）。无资产时**在规划阶段拒绝**，
    不留到生成期静默降级为文本页。
    """
    problems = []
    for sl in plan.get("slides", []) or []:
        if sl.get("page_type") != "image_caption":
            continue
        if _resolve_asset(sl.get("image_asset")) is None:
            problems.append(
                f"第 {sl.get('page')} 页声明 image_caption，但 image_asset "
                f"{sl.get('image_asset')!r} 不是可用受控资产"
                f"（只接受 {[str(b.relative_to(ROOT)) for b in CONTROLLED_ASSET_ROOTS]} 下的文件）；"
                "请在规划阶段改用受控资产或换页型，不得以文本页冒充。")
    return problems


def build_pptx(plan: dict, facts: list[dict], out_path: Path, theme_mod):
    """按计划生成原生 PPTX（文本 + 表格 + 指标卡），不复制已有页面凑数。

    每条 must_state 都**回绑**到冻结台账：标签取事实主体（subject），数值取原文原样
    （raw_value_text）。不凭空造标签，也不把数值重复成自己的标签。
    """
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from wengao_ppt import label_extract as label_mod
    from wengao_ppt import page_catalog as cat_mod

    # 全量标签一次算出（含同名消歧），图表/指标卡共用同一份
    _all_labels = label_mod.labels_for_facts(facts)

    # 图表关系证据（类别/系列/值/单位/来源 fact_id）：写入 run_record 供逐点复核
    chart_relations: list[dict] = []
    # 页型规则（容量/密度/回退/字号）的**实际调用**记录：证明规则由生成入口消费，
    # 而不是只在独立演示脚本里能画。
    page_type_decisions: list[dict] = []

    slides = plan["slides"]
    prs = Presentation()
    # 首发画布必须符合 contracts/support_matrix_v1.json 的
    # constraints.aspect_ratio_first_release = "16:9"。
    # python-pptx 默认是 4:3（9144000x6858000），直接沿用会违反契约；
    # 既有样稿（samples/material/source.pptx 等）用的是 12191695x6858000。
    prs.slide_width = CANVAS_16_9[0]
    prs.slide_height = CANVAS_16_9[1]
    theme = theme_mod.THEMES[theme_mod.DEFAULT_THEME_ID]
    colors = theme["colors"]

    # 回绑索引：数值原文 -> 事实（subject 供标签使用）
    by_token = {}
    for f in facts:
        tok = f.get("raw_value_text")
        if tok and tok not in by_token:
            by_token[tok] = f

    def _fact_by_sentence(s: str):
        """按**整句身份**回绑冻结事实（不是按数字巧合）。

        台账 fact 的 source_ref.sentence 与条目 strip 后一致（或台账原句恰为
        条目子串且长度足以确认）时，返回该 fact——它的 subject 就是该句真实的
        来源章节。P15/P16 的错章标签（数字 token 撞车）由此根治。
        """
        t = (s or "").strip()
        if not t:
            return None
        for f in facts:
            sent = ((f.get("source_ref") or {}).get("sentence") or "").strip()
            if sent and (sent == t or (len(sent) >= 12 and sent in t)):
                return f
        return None

    def resolve(item: str):
        """把一条 must_state 解析为 (标签, 数值)。

        **不做截断**：截断会把 "占比67%" 砍成 "占比6"，破坏事实保真。
        放不下时由上层分页续排，而不是截断内容。
        标签来源优先级：整句事实绑定（章节=事实 subject）→ 台账数值回绑
        （须句子确认，防数字巧合）→ 如实标注来源不明。绝不把 A 章节的
        subject 配到 B 章节的句子上。
        """
        s = (item or "").strip()
        if not s:
            return ("未命名指标", "")
        # 0) 整句身份绑定：subject 即该句真实来源章节（P15/P16 修复）
        f_sent = _fact_by_sentence(s)
        if "：" in s:
            label, _, val = s.partition("：")
            # 「标签：原句」形式的标签也必须与事实章节一致：若整句绑定命中
            # 且冒号前标签就是某章节名，以事实章节为准（防巧合章节串号）。
            subj = (f_sent or {}).get("subject")
            if subj and label.strip() and label.strip() != subj and subj.startswith(("一", "二", "三", "四", "五", "六", "七", "八", "九", "十")):
                return (subj, val.strip())
            return (label.strip(), val.strip())
        # 1) 整句绑定命中 → 用事实自身 subject 作标签
        if f_sent is not None and (f_sent.get("subject") or "").strip():
            return ((f_sent.get("subject") or "指标"), s)
        # 2) 形如「标签：数值」
        # 3) 整条是一个数值/数值+单位 → 用台账主体作标签，数值保持原样；
        #    **必须句子确认**：该 fact 的原句须真实出现在本条目中，
        #    禁止仅因数值 token 相同（如跨章节都出现「18」）就借用 subject。
        for tok, f in by_token.items():
            if not tok or tok not in s:
                continue
            sent = ((f.get("source_ref") or {}).get("sentence") or "").strip()
            if sent and (sent == s or sent in s):
                return ((f.get("subject") or "指标"), s)
        # 4) 含数值但不在台账 → 如实标注来源不明，保留完整原句
        mnum = re.search(r"-?\d[\d,]*\.?\d*\s*(?:%|个百分点|亿元|万元|亿|万|元|人|家|天|小时|分钟|倍|台|次|分)?", s)
        if mnum:
            return (s.replace(mnum.group(0), "").strip(" 的，、（）()") or "指标", s)
        return (s, "")

    def _rgb(h):
        return RGBColor.from_string(h)

    def _full_title(s):
        """标题占位符铺满画布宽度（默认模板按 10in 设计，16:9 上右侧空 3in）。"""
        ph = s.shapes.title
        if ph is not None:
            ph.left = Inches(0.7)
            ph.top = Inches(0.55)
            ph.width = Inches(11.9)
            ph.height = Inches(1.15)

    def add_cover(title, subtitle, facts_line=None):
        """封面：标题 + 副标题。封面**不承载必讲事实**——
        若计划把事实放进封面页，必须原样带到副标题区，不能只留标题。"""
        s = prs.slides.add_slide(prs.slide_layouts[0])
        s.shapes.title.text = title
        sub = s.placeholders[1]
        text = subtitle or ""
        if facts_line:
            text = (text + "\n" + facts_line).strip()
        # 默认封面副标题框只有 10in 宽且底边约 6.17in；24pt 下事实行
        # 会落到框外。为整句事实和换行留足真实几何空间，不以门禁容差放行。
        sub.left = Inches(0.7)
        sub.top = Inches(4.1)
        sub.width = Inches(11.9)
        sub.height = Inches(2.9)
        sub.text_frame.word_wrap = True
        from pptx.enum.text import MSO_ANCHOR
        sub.text_frame.vertical_anchor = MSO_ANCHOR.TOP
        sub.text_frame.text = text
        # 封面副标题与事实行显式 26pt（落在导语 24–28pt 角色范围内）。
        for para in sub.text_frame.paragraphs:
            para.line_spacing = 1.25
            for r in para.runs:
                r.font.size = Pt(26)
        return s

    #: 正文区几何（16:9）：top、框高、单栏宽、双栏宽。容量估算与布局共用。
    _BODY_TOP, _BODY_H = 1.9, 5.1
    _COL_FULL, _COL_HALF = 11.9, 5.85
    #: 中文行盒实测系数：字号估算按实际中文字体行盒留余量，防止边界行裁切。
    #: ≈0.554in（pt×1.25×1.45/72），比"pt×1.25/72"的 0.382in 高 ~45%——
    #: Songti/STHeiti 等中文字体的行距度量更大。容量估算必须用实测值，
    #: 否则"估高 4.17in/实际 5.55in"的偏差会让目录 8 条贴边或出界
    #: （监督 18:07 实证：30pt 目录第 7 条被切、第 8 条出画布）。
    _CJK_LINE_FACTOR = 1.45

    def _bullets_est_h(its, col_w_in: float, pt: float = 24.0) -> float:
        """估算一栏文本高度：每行 pt×1.25×实测行盒系数 + 段后 10pt(≈0.14in)。

        每行全角字符数按字号和栏宽动态计算；双栏改按 5.85in 估算。
        """
        per_line = max(8, int((col_w_in - 0.2) / (pt / 72.0)))
        return sum(max(1, -(-len(str(it)) // per_line)) * pt * 1.25 * _CJK_LINE_FACTOR / 72 + 0.14
                   for it in its)

    def _fill_bullets(tf, its):
        """把条目写进一个文本框架：24pt、1.25 行距、段后 10pt（显式值）。"""
        from pptx.oxml.ns import qn
        from lxml import etree
        tf.word_wrap = True
        tf.text = str(its[0])
        for it in its[1:]:
            p = tf.add_paragraph()
            p.text = str(it)
        # 占位符继承母版的项目符号；条目原句若已自带编号（"1. "/"2) "等，
        # 原句是冻结事实、不能改写去掉这个前缀），再叠一个继承的"•"就是
        # 双重编号。换行恰好落在编号前时，这个多出来的"•"会单独占一行
        # （2026-10-31 job-8227a4c941e4 第 13 页 HG08 ORPHAN_CHAR_LINE）。
        # 只在条目本身已有编号前缀时关闭继承项目符号，未编号的条目外观不变。
        numbered = re.compile(r"^\s*\d+[.\)、]\s")
        for it, para in zip(its, tf.paragraphs):
            if numbered.match(str(it)):
                pPr = para._p.get_or_add_pPr()
                etree.SubElement(pPr, qn("a:buNone"))
        for para in tf.paragraphs:
            para.line_spacing = 1.25
            para.space_after = Pt(10)
            for r in para.runs:
                r.font.size = Pt(24)

    def add_bullets(title, items):
        """正文页（含目录/议程页）：显式 24pt 正文（执行硬规则下限）。

        默认模板的占位符按 10 英寸画布设计，在 13.33×7.5 的 16:9 画布上
        右侧留 3 英寸空白——实测 PDF 显示"内容集中在上部、大片闲置"。
        单栏显式铺满可用版面（0.7→12.6 英寸）并设置 24pt、1.25 行距；
        字号是显式值，不依赖占位符继承；主题层 _capped 上限 24 不会削它。

        容量（监督 2026-10-20 18:07 实证）：单栏估高超过框高时改**双栏**
        （条目对半、栏宽 5.85in）——目录 8 条在 4+4 双栏下任何主题都有余量；
        双栏仍放不下的条目**交溢出由调用方续页**（原生表格页），
        不裁切、不删句、不缩字号、不复制填页。
        """
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = title
        _full_title(s)
        its = [str(x) for x in (items or []) if str(x).strip()]
        top, box_h = _BODY_TOP, _BODY_H
        overflow: list[str] = []
        if its and _bullets_est_h(its, _COL_FULL) > box_h and len(its) >= 4:
            # ---- 双栏：条目对半；第二栏超容的条目交溢出续页 ----
            half = -(-len(its) // 2)
            cols = [its[:half], its[half:]]
            kept: list[list[str]] = []
            for ci, col in enumerate(cols):
                if _bullets_est_h(col, _COL_HALF) <= box_h:
                    kept.append(col)
                elif ci == 0:
                    kept.append(col)      # 首栏必留（理论不可达：对半后仍超容）
                else:
                    overflow = col
            # 双栏页不留空占位符：layout[1] 自带的 body 占位符没有文字，
            # 会被自适应主题 refine 的 vcenter 规则误判为"卡片"（Sh.is_text
            # 要求 text 非空），把第一栏文字垂直居中进 1.9→7.0in 的空框，
            # 实测两栏错位 2.14in、左框底部 9.14in 出画布。占位符无任何
            # 文字，删除不丢事实；只删非标题占位符（idx≠0）。
            for ph in list(s.placeholders):
                if ph.placeholder_format.idx != 0:
                    ph._element.getparent().remove(ph._element)
            for ci, col in enumerate(kept):
                ph = s.shapes.add_textbox(Inches(0.7 + ci * 6.05), Inches(top),
                                          Inches(_COL_HALF), Inches(box_h))
                _fill_bullets(ph.text_frame, col)
                # 顶锚定（不用垂直居中）：文本从框顶开始排布，两栏同顶
                # （top 相同、字号相同 → 行盒一致，视觉严格对齐）。
            return s, overflow
        # ---- 单栏 ----
        body_ph = s.placeholders[1]
        body_ph.left = Inches(0.7)
        body_ph.top = Inches(top)
        body_ph.width = Inches(_COL_FULL)
        body_ph.height = Inches(box_h)
        _fill_bullets(body_ph.text_frame, its or ["（本页模型未给出要点，请人工补充）"])
        if not its or _bullets_est_h(its, _COL_FULL) <= 2.8:
            from pptx.enum.text import MSO_ANCHOR
            body_ph.text_frame.vertical_anchor = MSO_ANCHOR.MIDDLE
        return s, overflow

    def add_metrics(title, items):
        """指标卡页：**完整保留**每条 must_state 原文（不截断数值/单位）。

        标题行用「标签」，正文行放**完整原句**，确保事实一字不丢；
        一页放不下的条目以原生表格页续排。
        """
        s = prs.slides.add_slide(prs.slide_layouts[5])
        s.shapes.title.text = title
        _full_title(s)
        CAP = 4
        shown = items[:CAP]
        cols = 2 if len(shown) > 1 else 1
        for i, item in enumerate(shown):
            label, val = resolve(item)
            # 指标卡的标签用**指标名**（如"新增渠道合作伙伴"）；resolve 返回的
            # 章节主体（如"三、客户与生态"）与页标题重复，三卡同标签层级差。
            # 表格页仍保留章节主体（P9 已验收的章节绑定行为不变）。
            _f = _fact_for_item(item)
            _subj = ((_f or {}).get("subject") or "").strip()
            if _subj[:1] in "一二三四五六七八九十" and len(_subj) <= 12:
                _nicer = _metric_label_for(
                    ((_f or {}).get("raw_value_text") or val or ""), None, _f)
                if _nicer and _nicer.strip() and _nicer.strip() != _subj:
                    label = _nicer.strip()
            row, col = divmod(i, cols)
            left = Inches(0.7 + col * 6.2)
            top = Inches(2.0 + row * 2.5)
            box = s.shapes.add_textbox(left, top, Inches(5.95), Inches(2.3))
            tf = box.text_frame
            tf.word_wrap = True
            p1 = tf.paragraphs[0]
            run = p1.add_run(); run.text = label or "指标"
            run.font.size = Pt(20); run.font.bold = True
            run.font.color.rgb = _rgb(colors["primary"])
            p2 = tf.add_paragraph()
            p2.line_spacing = 1.2
            p2.space_before = Pt(6)
            # 保真（兑现本函数 docstring）：正文行渲染**完整原条目**。
            # 「标签：正文」被拆开后冒号丢失，完整句在成稿中不再连续，
            # 保真核对（FIDELITY.REQUIRED_TEXT_MISSING）必挂。
            _body = item if val else "（来源待补）"
            # 长句降字号，避免整句放回正文后撑爆卡片（HG05/HG06 溢出）。
            _blen = len(_body)
            _bsz = 24 if _blen <= 26 else (20 if _blen <= 48 else 18)
            r2 = p2.add_run(); r2.text = _body
            r2.font.size = Pt(_bsz); r2.font.color.rgb = _rgb(colors["text"])
        # 溢出条目携带**原始 must_state 字符串**，续排时不再拆解，
        # 避免把 "3.1 亿元" 拆成 "3.1" + "亿元" 而破坏事实。
        overflow = list(items[CAP:])
        return s, overflow

    def _metric_label_for(val_text: str, used: set, fact=None):
        """为一个数值取**语义标签**（复用产品模块 label_extract）。

        早先的实现在这里用正则抓"数字前那几个字"，实测产出「同比增长」「涉及」
        「低于」这类句中碎片，退化时落到「数量（台）」/「数值」——都不是类别。
        现在统一走 label_extract：优先冻结事实原句里的名词；
        取不到可靠短标签时给完整来源描述，并由关系核对按待复核处理。
        """
        if fact is not None:
            for r in _all_labels:
                if r.get("fact_id") == fact.get("fact_id"):
                    return r["label"]
            return label_mod.label_for_fact(fact)["label"]
        return label_mod.label_for_fact({
            "fact_id": None, "raw_value_text": val_text,
            "source_ref": {"locator": "", "sentence": val_text or ""},
        })["label"]

    def _fact_for_item(item: str):
        """把 must_state 条目回绑到**冻结事实本身**（不是标签结果）。

        标签与图表数值必须共用同一来源，否则会出现"标签来自 A、数值来自 B"。
        回绑优先**整句身份**（台账原句与条目一致），token 仅作兜底且须句子
        确认——禁止数字巧合把 A 章节事实绑到 B 章节句子。
        """
        t = (item or "").strip()
        f_sent = _fact_by_sentence(t)
        if f_sent is not None:
            return f_sent
        best = None
        best_len = 0
        for tok, f in by_token.items():
            if not tok or tok not in t:
                continue
            sent = ((f.get("source_ref") or {}).get("sentence") or "").strip()
            if sent and (sent == t or sent in t) and len(tok) > best_len:
                best, best_len = f, len(tok)
        return best

    def _fact_for_number(raw: str, item: str):
        """按**该数字本身**回绑冻结事实；绑不到就返回 None（该点不画）。

        早期实现按整条 item 回绑 → 同句两个事实（"74%，低于 85% 的目标"）
        会共用一个标签；更糟的是会按"item 里最长的 token"绑到**别的**事实，
        把 A 的标签配到 B 的数值上（主体对调）。
        """
        key = re.sub(r"[\s,%％,]", "", raw or "")
        if not key:
            return None
        exact, numeric = [], []
        for f in facts:
            rv = f.get("raw_value_text") or ""
            if rv and re.sub(r"[\s,%％,]", "", rv) == key:
                exact.append(f)
            m = re.match(r"[-+]?[\d,]*\.?\d+", rv)
            if m and m.group(0).replace(",", "") == key.replace(",", ""):
                numeric.append(f)
        pool = exact or numeric
        if not pool:
            return None                 # 台账里没有这个数值 → 不冒充、不画
        if len(pool) == 1:
            return pool[0]
        for f in pool:                  # 同值多事实：取原句 token 命中者
            if (f.get("raw_value_text") or "") in item:
                return f
        return pool[0]

    def _role_marker(sentence: str, raw: str) -> str:
        """从原句中该数值所在（或紧随）的分句取**比较角色**，不猜。"""
        s = sentence or ""
        if not s or not raw:
            return ""
        idx = s.find(raw)
        if idx < 0:
            return ""
        clauses = [c for c in re.split(r"[，,；;。]", s) if c.strip()]
        ci, pos = 0, 0
        for k, c in enumerate(clauses):
            if pos <= idx < pos + len(c):
                ci = k
                break
            pos += len(c) + 1
        own = clauses[ci] if ci < len(clauses) else ""
        nxt = clauses[ci + 1] if ci + 1 < len(clauses) else ""
        if any(c in own for c in ("目标", "计划", "预计", "承诺", "预算")):
            return "目标"
        if any(c in nxt for c in ("目标", "计划", "预计", "承诺")) and any(
                c in nxt for c in ("低于", "高于", "未达", "超过", "达到")):
            return "当前"
        if any(c in own for c in ("同比", "环比")):
            return "同比"
        if any(c in own for c in ("下降", "减少", "降低", "下滑", "负增长", "亏损")):
            return "对比"
        if any(c in own for c in ("截至", "上半年", "下半年", "全年")):
            return "期间"
        return ""

    def _strip_foreign_numbers(label: str, own_raw: str) -> str:
        """从类别标签里剥掉**不属于本点数值**的数字 token。

        同句双数值（"…覆盖率为 74%，低于 85% 的目标"）给两个点共用同一
        语义标签；label_extract 取句首名词时把"74%"一并带进标签，于是 85
        的"目标"点被错标成"…74%·目标"（把当前值误当目标值的一部分）。
        这里剔除与本点数值不同的数字片段；本点自己的数值与语义文字保留。
        """
        out = label or ""
        own = re.sub(r"[\s,%％]", "", own_raw or "")
        for tok in re.findall(r"-?\d[\d,]*(?:\.\d+)?\s*%?", out):
            t = re.sub(r"[\s,%％]", "", tok)
            if t and t != own:
                out = out.replace(tok, "")
        out = re.sub(r"·{2,}", "·", out).strip("· ").strip()
        return out or (label or "")

    def add_chart(title, items, chart_type="column"):
        """生成**真实原生图表**（非图片、非文本页）。

        关系要求（§3，全部由核对模块逐点验证）：
        - 类别标签必须**可区分数据点**且绑定到该数值**自己**的冻结事实；
        - 系列名不得是「数值」这类通用词，须承载指标/单位语义；
        - 同句多事实按各自角色区分（当前 / 目标 / 同比 / 对比）；
        - 重复的 (类别, 数值) 不再重复绘制（防灌水凑覆盖）；
        - 图表未画出的条目以原生表格续页列出，绝不丢弃必讲事实。
        """
        from pptx.chart.data import CategoryChartData
        from pptx.enum.chart import XL_CHART_TYPE
        # ---- 第一遍：收集全部候选点（每个数值绑定各自冻结事实）----
        # 一句可含多个数值，每个数值都带自己的 unit_class；**先收集、后分组**，
        # 同一数值轴只允许出现同一单位类（可比较）的点。
        cand = []          # {cat_base, num, unit, fact, raw, item}
        rest = []
        used_labels: dict[str, str] = {}
        seen_items = set()
        for item in items:
            if item in seen_items:
                continue          # 同一原句被重复分配 → 只处理一次
            seen_items.add(item)
            label, val = resolve(item)
            src = val or ""
            nums = re.findall(r"-?\d[\d,]*(?:\.\d+)?", src)
            item_added = False
            for raw in nums:
                try:
                    num = float(raw.replace(",", ""))
                except ValueError:
                    continue
                bound = _fact_for_number(raw, item)
                if bound is None:
                    if not item_added:
                        rest.append(item)     # 台账里没有这个数值 → 不画，改列原文
                        item_added = True
                    continue
                sent = ((bound or {}).get("source_ref") or {}).get("sentence") or item
                base = _metric_label_for(raw, used_labels, bound)
                role = _role_marker(sent, raw)
                if role:
                    # 同句多角色点共用标签：剥掉不属于本点数值的数字（85 的
                    # 目标点不得带"74%"后缀）
                    base = _strip_foreign_numbers(base, raw)
                    # 基名已带同一角色（如规则5的"·改善前"）时不重复追加
                    if not base.endswith("·" + role):
                        base = f"{base}·{role}"
                cat = base
                unit = (bound or {}).get("unit_class") or "unknown"
                cand.append({"cat_base": cat, "num": num, "unit": unit,
                             "fact": bound, "raw": raw, "item": item})
                item_added = True
            if not item_added and not nums:
                rest.append(item)
        if not cand:
            # 先确认存在可画数值再创建图表页，避免纯文字主题留下空图表壳，
            # 同时完整保留文本回退在原页承载，不改变计划页数。
            return add_bullets(title, items or ["（本页无可绘制数值）"])
        # ---- 按单位类分组：数值轴只画**同一单位**的可比点 ----
        # 3 分钟、22 个、74% 量纲不同，同轴绘制即是误导；74% 与 85% 同为
        # 百分比才可同轴。其余组的条目**整句进 rest**，同页表格列出，不删事实。
        groups: dict[str, list[dict]] = {}
        order_units: list[str] = []
        for c in cand:
            u = c["unit"]
            if u not in groups:
                groups[u] = []
                order_units.append(u)
            groups[u].append(c)
        plot_unit = max(order_units, key=lambda u: (len(groups[u]),
                                                    -order_units.index(u)))
        plotted = groups[plot_unit]
        for u in order_units:
            if u == plot_unit:
                continue
            for c in groups[u]:
                if c["item"] not in rest:
                    rest.append(c["item"])
        # ---- 组内绘制（容量上限、类别消歧、重复点不重画）----
        CAP = 6
        cats, vals, plotted_facts = [], [], []
        plotted_raws: list[str] = []
        plotted_cats: list[str] = []
        plotted_pairs = set()
        plotted_items_order: list[str] = []   # 本图实际绘制的条目（保持顺序）
        for c in plotted:
            cat, raw, num, bound = c["cat_base"], c["raw"], c["num"], c["fact"]
            if used_labels.get(cat) not in (None, raw):
                # 同一类别标签已被不同数值占用：**不得**用"标签+括号数值"凑类别
                # （监督 2026-10-20：截断字符串+括号数值不是可区分的类别）。
                # 无依据区分时该项整句进同页原生表格，保留原句，不制造语义。
                if c["item"] not in rest:
                    rest.append(c["item"])
                continue
            pair = (cat, num)
            if pair in plotted_pairs:
                continue      # 重复点不重复绘制
            if len(cats) >= CAP:
                if c["item"] not in rest:
                    rest.append(c["item"])
                continue
            if c["item"] not in plotted_items_order:
                plotted_items_order.append(c["item"])
            cats.append(cat)
            plotted_cats.append(cat)
            plotted_raws.append(raw)
            vals.append(num)
            plotted_facts.append(bound)
            used_labels[cat] = raw
            plotted_pairs.add(pair)
        if not vals:
            return add_bullets(title, items or ["（本页无可绘制数值）"])
        # 只在确认至少一个有效数据点之后才创建原生图表页。
        s = prs.slides.add_slide(prs.slide_layouts[5])
        s.shapes.title.text = title
        _full_title(s)
        # 单组内单位已一致：系列名承载该单位语义
        if plot_unit in ("", "unknown"):
            series_name = "指标（单位见来源台账）"
            num_fmt = "General"
        else:
            series_name = f"指标（{plot_unit}）"
            # Preserve the precision stated in source facts. Integers such as 74%
            # must not be rendered as 74.0%, while 12.8 亿元 keeps one decimal.
            decimal_places = 0
            for raw_value in plotted_raws:
                m_dec = re.search(r"[-+]?\d[\d,]*(?:\.(\d+))?", str(raw_value or ""))
                if m_dec:
                    decimal_places = max(decimal_places, len(m_dec.group(1) or ""))
            number_pattern = "0" + ("." + "0" * decimal_places if decimal_places else "")
            num_fmt = f'{number_pattern}"{plot_unit}"'
        # 图表类别若由同一指标前缀加当前/目标等角色组成，只在图上显示角色，
        # 把指标名作为独立标题，避免长中文类别被 LibreOffice 旋转成竖排。
        # 完整类别与来源句仍记录在 chart_relations / 同页来源原句中。
        role_labels = {"当前", "目标", "改善前", "改善后", "同比", "环比",
                       "同期", "预算", "实际", "去年", "今年", "上年", "本年"}
        cat_parts = [c.rsplit("·", 1) if "·" in c else ("", c) for c in cats]
        prefixes = {p for p, _suffix in cat_parts}
        compact_metric = None
        display_cats = list(cats)
        if (len(prefixes) == 1 and "" not in prefixes
                and all(suffix in role_labels for _prefix, suffix in cat_parts)):
            compact_metric = next(iter(prefixes))
            display_cats = [suffix for _prefix, suffix in cat_parts]

        cd = CategoryChartData()
        cd.categories = display_cats
        cd.add_series(series_name, tuple(vals))
        kind = {"column": XL_CHART_TYPE.COLUMN_CLUSTERED,
                "bar": XL_CHART_TYPE.BAR_CLUSTERED,
                "line": XL_CHART_TYPE.LINE_MARKERS}.get(chart_type,
                                                        XL_CHART_TYPE.COLUMN_CLUSTERED)
        # 指标名放入原生图表标题，不能另叠在图表边界上；标题随图表一起
        # 参与可见文字、遮挡与来源关系检查。
        # 若少量异量纲事实不能进同一坐标轴，优先留在图表页的原生宽表中；
        # 先给图表 + 来源说明 + 两条完整事实预留同页空间，避免把续表错误地
        # 追加到结论之后，随后又因请求页数不符而整项拒绝。
        inline_rest_candidate = bool(rest) and len(rest) <= 2
        chart_top = 2.12
        chart_height = 2.53 if inline_rest_candidate else 4.03
        gf = s.shapes.add_chart(kind, Inches(0.6), Inches(chart_top),
                                Inches(12.2), Inches(chart_height), cd)
        try:
            chart = gf.chart
            chart.has_title = bool(compact_metric)
            if compact_metric:
                chart.chart_title.text_frame.text = compact_metric
                for title_para in chart.chart_title.text_frame.paragraphs:
                    for title_run in title_para.runs:
                        title_run.font.size = Pt(18)
                        title_run.font.bold = True
            chart.has_legend = False
            plot = chart.plots[0]
            plot.has_data_labels = True
            dl = plot.data_labels
            dl.show_value = True            # 可见标签带值，单位随数值格式
            dl.number_format = num_fmt
            dl.number_format_is_linked = False
            dl.font.size = Pt(18)           # 图表数值标签执行 18pt 硬下限
            chart.font.size = Pt(18)
            chart.category_axis.tick_labels.font.size = Pt(18)
            chart.value_axis.tick_labels.font.size = Pt(18)
        except Exception:  # noqa: BLE001
            pass
        # 数值轴：**0 起点**（柱高不夸大差异）；百分比量纲固定 0–100%。
        # 实测 LibreOffice 自动轴从 ~68 起，74 与 85 的差距被视觉放大。
        # 非百分比量纲此前从不设 maximum_scale：chart XML 不带 <c:max>，
        # scripts/layout_gate.py 的 HG02 轴范围检查读不到范围只能判
        # UNVERIFIED（证据/检查异常不算通过，下载与正式放行保持阻断），
        # 即便图表本身渲染完全正常（2026-11-02 job-mt-60a2dcd8/
        # job-57ce106e5450 实测：3 个非百分比图表全部命中）。用原生构建器
        # （wengao_ppt/builders/chart.py）同一套"必然包住数据"算法补上限，
        # 不需要和 LibreOffice 自己的自动轴选择完全一致，只需要包住它。
        try:
            va = chart.value_axis
            va.minimum_scale = 0.0
            if re.search(r"%|百分", plot_unit or ""):
                va.maximum_scale = 100.0
            else:
                from wengao_ppt.builders.chart import _nice_axis_bounds
                _, hi = _nice_axis_bounds(vals)
                va.maximum_scale = hi
        except Exception:  # noqa: BLE001
            pass
        # 关系证据：逐点记录 类别/系列/值/单位/来源 fact_id，并记录量纲分组
        # 决策（哪些单位被排除出本图、以同页表格承载），供独立复核可比性。
        chart_relations.append({
            "page_title": title,
            "chart_part_hint": f"slide {len(prs.slides._sldIdLst)}",
            "chart_title": compact_metric or "",
            "series_name": series_name,
            "axis_unit": plot_unit,
            "excluded_units_same_page_table": [u for u in order_units
                                               if u != plot_unit],
            "unplotted_statements_same_page_table": list(rest)
                if inline_rest_candidate else [],
            "points": [{"category": cats[i], "display_category": display_cats[i],
                        "value": vals[i],
                        "unit": ((plotted_facts[i] or {}).get("unit_class")
                                 or "unknown") if i < len(plotted_facts) else "unknown",
                        "fact_id": (plotted_facts[i] or {}).get("fact_id")
                        if i < len(plotted_facts) else None}
                       for i in range(len(vals))],
        })
        # 来源与限定（原句）注脚：图上只出现"标签+数值"，原句里的限定/原因
        # （如"客户成功团队人手不足"）若不上屏就会丢语义——数值覆盖不能
        # 替代语义保留。把**本图各点的冻结原句**去重后整句放在图表下方（16pt）。
        src_lines: list[str] = []
        for it in plotted_items_order:
            f = _fact_for_item(it)
            sent = (((f or {}).get("source_ref") or {}).get("sentence") or it or "").strip()
            if sent and sent not in src_lines:
                src_lines.append(sent)
        src_height = sum(max(1, -(-len(("来源与限定（原句）：" if i == 0 else "") + line) // 54))
                         * 16 * 1.15 * 1.45 / 72 + 0.05
                         for i, line in enumerate(src_lines[:3]))

        def _inline_row_height(text: str) -> float:
            per_line = max(1, int((11.9 - 0.2) * 72 / 20))
            lines = max(1, -(-len(str(text or "")) // per_line))
            return max(0.62, lines * 20 * 1.2 * 1.45 / 72 + 0.14)

        inline_table_height = sum(_inline_row_height(x) for x in [
            "同页完整事实（未绘入图表）", *rest]) if inline_rest_candidate else 0.0
        inline_note_height = max(0.45, src_height + 0.12)  # +0.12：文本框默认上下 inset，防视觉裁切
        inline_table_top = 4.90 + inline_note_height + 0.10
        inline_rest = (inline_rest_candidate and len(src_lines) <= 3
                       and src_height <= 0.90
                       and inline_table_top + inline_table_height <= 7.38)

        if inline_rest:
            if src_lines:
                nb = s.shapes.add_textbox(Inches(0.6), Inches(4.84),
                                          Inches(12.2), Inches(inline_note_height))
                ntf = nb.text_frame
                ntf.word_wrap = True
                for i, snt in enumerate(src_lines[:3]):
                    para = ntf.paragraphs[0] if i == 0 else ntf.add_paragraph()
                    para.line_spacing = 1.15
                    run = para.add_run()
                    run.text = (("来源与限定（原句）：" if i == 0 else "") + snt)
                    run.font.size = Pt(16)

            row_texts = ["同页完整事实（未绘入图表）", *rest]
            row_heights = [_inline_row_height(x) for x in row_texts]
            shape = s.shapes.add_table(len(row_texts), 1, Inches(0.7),
                                       Inches(inline_table_top), Inches(11.9),
                                       Inches(sum(row_heights)))
            tbl = shape.table
            tbl.columns[0].width = Inches(11.9)
            for row, row_h in zip(tbl.rows, row_heights):
                row.height = Inches(row_h)
            for i, (row, text) in enumerate(zip(tbl.rows, row_texts)):
                tbl.cell(i, 0).text = text
                for cell in row.cells:
                    for para in cell.text_frame.paragraphs:
                        para.line_spacing = 1.2
                        for run in para.runs:
                            run.font.size = Pt(20)
                            if i == 0:
                                run.font.bold = True
            over_rest: list[str] = []
        else:
            # 更大的溢出仍走显式原生续表；绝不缩字/丢事实。当前图表页如有
            # 两条内联事实候选但实际容量不足，也会保留失败页数信号供上层阻断。
            over_rest = list(rest)
            if src_lines and src_height > 0.90:
                over_rest.extend("来源与限定（原句）：" + line for line in src_lines)
                src_lines = []
            if src_lines:
                # 注脚框高度按内容估算，顶部贴图表底沿。原先固定 1.05in
                # 把框底压到 7.37in，距页脚安全线只剩 0.022in，触发
                # HG06 CONTENT_FOOTER_GAP（要求 ≥0.12in）。
                _foot_top = chart_top + chart_height + 0.07
                _foot_h = max(0.45, min(src_height + 0.12, 7.27 - _foot_top))
                nb = s.shapes.add_textbox(Inches(0.6), Inches(_foot_top),
                                          Inches(12.2), Inches(_foot_h))
                ntf = nb.text_frame
                ntf.word_wrap = True
                for i, snt in enumerate(src_lines[:3]):
                    para = ntf.paragraphs[0] if i == 0 else ntf.add_paragraph()
                    para.line_spacing = 1.15
                    run = para.add_run()
                    run.text = (("来源与限定（原句）：" if i == 0 else "") + snt)
                    run.font.size = Pt(16)
        return s, over_rest

    def add_table(title, items):
        """原生单列表格：每一格只放完整冻结原句，不截断数字或生成悬空标签。

        表宽铺满 0.7→12.6 英寸；正文与表头均为 20pt，长句按单列真实宽度
        估行高并续页，避免短标签丢数字、窄列堆叠或缩小字号。
        """
        s = prs.slides.add_slide(prs.slide_layouts[5])
        s.shapes.title.text = title
        _full_title(s)
        # 行高预算：11.7in 单列 20pt；放不下的行交溢出续页（原生表格页），
        # 不删事实、不缩字号、不裁切（监督 2026-10-20 18:07 遮挡实证的同类防护）。
        def _tbl_row_h(text: str) -> float:
            per_line = max(1, int((11.9 - 0.2) * 72 / 20))
            lines = max(1, -(-len(str(text or "")) // per_line))
            return max(0.62, lines * 20 * 1.2 * 1.45 / 72 + 0.14)
        overflow_rows: list[str] = []
        budget_bottom = 7.30                  # 表下沿上限（画布 7.5 内留余量）
        used = 0.60                           # 表头
        kept = []
        for item_index, it in enumerate(items):
            h = _tbl_row_h(it)
            if used + h > budget_bottom - 1.75:
                # Keep source order across physical pages. Once one row does not
                # fit, all following rows continue after it; skipping ahead here
                # would reorder facts in the rendered deck.
                overflow_rows = list(items[item_index:])
                break
            used += h
            kept.append(it)
        if items and not kept:
            raise RuntimeError(
                "TABLE_ROW_EXCEEDS_PAGE_CAPACITY: a complete source statement "
                "cannot fit at the minimum readable table size")
        row_heights = [_tbl_row_h("完整原句（冻结事实）")] + [_tbl_row_h(x) for x in kept]
        shape = s.shapes.add_table(len(kept) + 1, 1, Inches(0.7), Inches(1.75),
                                   Inches(11.9), Inches(sum(row_heights)))
        tbl = shape.table
        tbl.columns[0].width = Inches(11.9)
        for row, row_h in zip(tbl.rows, row_heights):
            row.height = Inches(row_h)
        tbl.cell(0, 0).text = "完整原句（冻结事实）"
        for i, raw in enumerate(kept, start=1):
            tbl.cell(i, 0).text = raw
        for r_idx, row in enumerate(tbl.rows):
            for cell in row.cells:
                for para in cell.text_frame.paragraphs:
                    para.line_spacing = 1.2
                    for run in para.runs:
                        run.font.size = Pt(20)
                        if r_idx == 0:
                            run.font.bold = True
        return s, overflow_rows, kept

    def add_image_caption(title, items, asset=None, caption=None):
        """图片说明页：必须使用**受控本地资产**渲染真实图片 + 说明。

        无受控资产时**不在生成阶段静默降级为文本页**——由规划阶段/入口拒绝
        （见 `validate_plan_assets`）。这里只在拿到资产时才会被调用。
        """
        if not asset:
            raise RuntimeError("image_caption 页缺少受控本地资产；不得以文本页冒充。")
        s = prs.slides.add_slide(prs.slide_layouts[5])
        s.shapes.title.text = title
        s.shapes.add_picture(str(asset), Inches(1.2), Inches(1.7),
                             width=Inches(9.5))
        cap = caption or " · ".join(items) if items else (caption or "")
        box = s.shapes.add_textbox(Inches(1.2), Inches(6.0), Inches(9.5), Inches(0.9))
        box.text_frame.word_wrap = True
        box.text_frame.text = f"图：{cap}" if cap else "图："
        return s

    # 12 类页型的**统一入口名**与 D10 目录键的映射（实际生成入口消费该目录）
    _CAT_KEY = CATALOG_KEY_MAP

    rendered_page_manifest: list[dict] = []
    planned_page_to_actual: dict[int, int] = {}

    def emit_overflow_pages(source_title: str, source_page_type: str,
                            source_planned_page: int | None,
                            source_actual_page: int, overflow_items: list[str]):
        """Render and record table continuations immediately after their source.

        Every continuation is an explicit physical page in the returned manifest.
        Long continuations split by the same table fitter and remain in source
        order. Non-shrinking layout failures stop the build instead of looping.
        """
        pending = list(overflow_items)
        title2 = _continued_title(source_title)
        sequence = 0
        while pending:
            before = len(prs.slides._sldIdLst)
            _, remaining, kept = add_table(title2, pending)
            after = len(prs.slides._sldIdLst)
            if after != before + 1:
                raise RuntimeError(
                    "continuation table did not produce exactly one physical slide")
            if not kept or kept + remaining != pending:
                raise RuntimeError(
                    "continuation table did not preserve every source row in order")

            sequence += 1
            actual_page = after
            page_spec = {
                "page": actual_page,
                "page_type": "table",
                "title": title2,
                "must_state": list(kept),
                "_continued_from": source_actual_page,
                "_builder_generated_continuation": {
                    "source_planned_page": source_planned_page,
                    "source_actual_page": source_actual_page,
                    "source_page_type": source_page_type,
                    "sequence": sequence,
                    "source_items": len(overflow_items),
                },
            }
            rendered_page_manifest.append(page_spec)
            fit = cat_mod.fit("table", len(kept))
            page_type_decisions.append({
                "page": actual_page, "page_type": "table",
                "catalog_key": "table", "units": len(kept),
                "density": fit["density"], "layout": fit["layout"],
                "fallback_chain": fit["fallback_chain"],
                "body_pt": fit["font_profile"]["body_pt"],
                "min_body_pt": fit["font_profile"]["min_body_pt"],
                "font_ok": fit["font_ok"],
                "capacity_conflict": fit["capacity_conflict"],
                "generated_continuation": True,
            })
            pending = remaining

    for sl in slides:
        t = sl["page_type"]
        title = sl["title"]
        items = sl.get("must_state") or []
        before = len(prs.slides._sldIdLst)
        over: list[str] = []
        # 实际调用 D10 页型目录：得到密度档、布局、回退链与字号下限。
        cat_key = _CAT_KEY.get(t, "single_column")
        units = len(items)
        fit = cat_mod.fit(cat_key, units)
        page_type_decisions.append({
            "page": before + 1, "page_type": t, "catalog_key": cat_key,
            "units": units, "density": fit["density"], "layout": fit["layout"],
            "fallback_chain": fit["fallback_chain"],
            "body_pt": fit["font_profile"]["body_pt"],
            "min_body_pt": fit["font_profile"]["min_body_pt"],
            "font_ok": fit["font_ok"],
            "capacity_conflict": fit["capacity_conflict"],
        })
        if t == "cover":
            add_cover(title, "由原始材料生成 · " + _truncate_title_visible_units(
                          plan["deck_title"], 40),
                      facts_line="\n".join(items) if items else None)
        elif t == "metrics":
            _, over = add_metrics(title, items or [plan["deck_title"]])
        elif t == "chart":
            _, over = add_chart(title, items)
        elif t == "table":
            _, over, _kept = add_table(title, items or ["数据：见来源台账"])
        elif t == "image_caption":
            asset = _resolve_asset(sl.get("image_asset"))
            if asset is None:
                # 规划阶段本应拒绝；到这里说明计划未经入口校验 → 明确失败，不静默降级
                raise RuntimeError(
                    f"第 {sl.get('page')} 页声明 image_caption 但没有可用的受控本地资产"
                    f"（image_asset={sl.get('image_asset')!r}）")
            add_image_caption(title, items, asset=asset,
                              caption=sl.get("image_caption"))
        else:
            _, over = add_bullets(title, items or ["（本页模型未给出要点，请人工补充）"])

        after = len(prs.slides._sldIdLst)
        if after != before + 1:
            raise RuntimeError(
                f"planned page {sl.get('page')} rendered {after - before} "
                "primary slides; the page manifest cannot be reconciled")
        actual_page = after
        planned_page = sl.get("page") if isinstance(sl.get("page"), int) else None
        page_spec = dict(sl)
        page_spec["page"] = actual_page
        old_parent = page_spec.get("_continued_from")
        if isinstance(old_parent, int):
            page_spec["_continued_from"] = planned_page_to_actual.get(
                old_parent, old_parent)
        rendered_page_manifest.append(page_spec)
        if planned_page is not None:
            planned_page_to_actual[planned_page] = actual_page
        if over:
            emit_overflow_pages(title, t, planned_page, actual_page, over)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 保存前硬断言：画布必须是冻结的 16:9，杜绝沿用 python-pptx 的 4:3 默认值。
    ratio = prs.slide_width / prs.slide_height
    if abs(ratio - 16 / 9) > 0.01:
        raise RuntimeError(
            f"画布比例 {ratio:.4f} 不符合契约 16:9；"
            f"当前 {prs.slide_width}x{prs.slide_height} EMU")
    prs.save(str(out_path))
    prs._chart_relations = chart_relations
    prs._page_type_decisions = page_type_decisions
    prs._rendered_page_manifest = rendered_page_manifest
    return prs


# ----------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--material", required=True)
    ap.add_argument("--pages", type=int, default=12)
    ap.add_argument("--model", default="qwen3.8:27b-wb65536")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--single-pass", action="store_true",
                    help="单次规划（短材料对照用）；默认两阶段：全局结构 + 分块填充")
    ap.add_argument("--cancel-file", default=None,
                    help="若该文件存在则立即停止：取消后不继续构建/接纳/导出")
    ap.add_argument("--replay-plan", default=None,
                    help="回放分页前的冻结内容计划，仅跳过内容规划模型；下游语义配图仍可能调用本地模型并查询图库。仅演示/回归用。")
    args = ap.parse_args()

    mat = Path(args.material)
    if not mat.is_absolute():
        mat = ROOT / mat
    outdir = Path(args.outdir) if args.outdir else OUTROOT / (mat.stem + "_run")
    if not outdir.is_absolute():
        outdir = ROOT / outdir
    outdir.mkdir(parents=True, exist_ok=True)

    def rel(p: Path) -> str:
        """相对项目根展示；落在项目根之外时如实给出绝对路径。

        材料输入完全可以在项目根之外（例如独立副本里的输入）——不得因此抛
        ValueError（`/tmp` 与 `/private/tmp` 这类等价路径尤其常见）。
        """
        try:
            return str(p.resolve().relative_to(ROOT))
        except ValueError:
            return str(p.resolve())

    from wengao_ppt import theme as theme_mod
    from wengao_ppt import label_extract as label_mod

    run = {"schema_version": "1.0.0",
           "started_at": now_iso(),
           "material": rel(mat),
           "material_sha256": sha256_file(mat),
           "target_pages": args.pages,
           "model": args.model,
           "budget": BUDGET,
           "note": "模型输出只当数据；不执行其命令/XML；不写 status/passed/verdict。"}

    # 1) 提取（复用既有提取器）
    doc = extractors.extract_any(mat)
    n_units = sum(len(_as_dict(p).get("units") or []) for p in (getattr(doc, "pages", []) or []))
    run["extraction"] = {
        "input_kind": doc.input_kind,
        "input_sha256": doc.input_sha256,
        "pages": len(getattr(doc, "pages", []) or []),
        "units": n_units,
        "tables": len(getattr(doc, "tables", []) or []),
        "extractor": "wengao_ppt.extractors.extract_any（复用既有，未另建引擎）",
        "warnings": list(getattr(doc, "warnings", []) or []),
    }

    def cancelled() -> bool:
        """取消检查：复用与 runner 取消同构的 `.cancelled` 标记文件语义。"""
        if not args.cancel_file:
            return False
        return Path(args.cancel_file).exists()

    def save_run(extra_verdict=None):
        run["finished_at"] = now_iso()
        if extra_verdict:
            run["verdict"] = extra_verdict
        (outdir / "run_record.json").write_text(
            json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")

    # 2) 必讲事实台账（**模型调用之前**冻结）。
    #    全量候选先入账（可审计），再按冻结页数契约做**显式容量预算**：
    #    大材料全量必讲与"页数区间 10—20"数学上互斥（2026-10-27 mt-c4b795f2
    #    90 页 docx → 562 条候选 → 152 页），必须在冻结时刻取舍并逐条留痕，
    #    而不是分页后才以 PAGE_RANGE_UNSATISFIED 失败。
    candidates = build_fact_ledger(doc)
    facts, fact_budget = budget_must_teach_facts(candidates, args.pages)
    fp = outdir / "fact_ledger.json"
    # 台账文件的双层语义：
    #   facts           = 本轮**必讲子集**（模型调用之前冻结；下游来源侧保真
    #                     核对 check_fact_coverage 的期望集合就是它——
    #                     2026-10-27 mt-76244dad：若把 562 条全量候选当期望，
    #                     预算后的 12 页稿会被判 416 条 REQUIRED_TEXT_MISSING）；
    #   candidate_facts = 全量候选（审计留痕，不作为期望集合）。
    fp.write_text(json.dumps({"schema_version": "1.0.0",
                              "frozen_at": now_iso(),
                              "frozen_before_model_call": True,
                              "count": len(facts), "facts": facts,
                              "budget": {k: v for k, v in fact_budget.items()
                                         if k != "omitted_facts"},
                              "candidate_count": len(candidates),
                              "candidate_facts": candidates,
                              "budget_omitted_facts": fact_budget["omitted_facts"]},
                             ensure_ascii=False, indent=2), encoding="utf-8")
    run["fact_ledger"] = {"path": rel(fp), "count": len(candidates),
                          "selected": fact_budget["selected"],
                          "frozen_before_model_call": True}
    run["fact_budget"] = {k: v for k, v in fact_budget.items()
                          if k != "omitted_facts"}
    if fact_budget.get("activated"):
        run["fact_budget"]["omitted_facts_path"] = rel(fp) + "#budget_omitted_facts"
        print(f"[facts] 候选 {fact_budget['candidates']} 条，页数预算后必讲 "
              f"{fact_budget['selected']} 条（章节 {fact_budget['subjects_selected']}"
              f"/{fact_budget['subjects_total']}，落选逐条留痕）；"
              f"不预算约 {fact_budget['estimated_pages_unbudgeted']} 页 > 冻结上限 "
              f"{PAGE_RANGE[1]}", flush=True)
    else:
        print(f"[facts] {len(facts)} 条已冻结 → {rel(fp)}", flush=True)

    # 3) 全局内容计划（模型）。默认按章节确定性注入冻结原句；--single-pass 用于短材料对照。
    already_paginated = False
    if args.replay_plan:
        # 回放只替代内容规划模型；下游语义配图仍按产品规则运行本地模型与图库检索。
        #   ① 分页前计划（无 _pagination）→ 走正常分页；
        #   ② 已分页计划（含 _pagination）→ 直接按既有分页构建，不二次分页。
        rp = Path(args.replay_plan)
        if not rp.is_absolute():
            rp = ROOT / rp
        plan = json.loads(rp.read_text(encoding="utf-8"))
        already_paginated = bool(plan.get("_pagination"))
        trace = [{"phase": "replay", "attempt": 1, "source": rel(rp),
                  "already_paginated": already_paginated}]
        mode = "replay"
        run["replay"] = {"frozen_plan": rel(rp), "sha256": sha256_file(rp),
                         "model_calls": 0, "model_call_scope": "content_plan",
                         "content_plan_model_calls": 0,
                         "downstream_model_stages": ["semantic_scene_planning"],
                         "downstream_photo_search_may_use_network": True,
                         "already_paginated": already_paginated,
                         "note": "只回放冻结内容计划，跳过内容规划模型；下游语义配图仍可能调用本地模型，照片检索可能联网。不得当作新的内容规划模型或真实生成能力证据。"}
    elif args.single_pass:
        mode = "single_pass"
        try:
            plan, trace = make_plan(args.model, facts, args.pages)
        except ModelCallError as exc:
            run["model_error"] = exc.as_record()
            run["plan"] = {"mode": mode, "failed": True,
                           "error": exc.code, "trace": []}
            save_run("PLAN_FAILED")
            print(f"[plan-error] {exc.code}: {exc.detail}", file=sys.stderr)
            return 0
    else:
        mode = "chunked"
        try:
            plan, trace = make_plan_chunked(args.model, facts, args.pages)
        except ModelCallError as exc:
            run["model_error"] = exc.as_record()
            run["plan"] = {"mode": mode, "failed": True,
                           "error": exc.code, "trace": []}
            save_run("PLAN_FAILED")
            print(f"[plan-error] {exc.code}: {exc.detail}", file=sys.stderr)
            return 0

    # 3b) 确定性补全 + 计划级门禁复验（**不调用模型**）：
    #     - 半句补全：条目是冻结原句的截断前缀 → 换完整原句；
    #     - 定性必讲句补入主体对应内容页（无数字的完整句子也要覆盖）；
    #     - 复验 validate_plan（含新增的定性覆盖门禁与半句门禁）。
    #     回放冻结计划与模型新计划走同一函数；回放的是**已分页**计划时，
    #     补全若改变了任何条目就退掉 _pagination 重新分页（分页是确定性的），
    #     不把超容量页面悄悄放出。
    pre_completion_status = {
        "failed": bool(plan.get("_failed")),
        "error": str(plan.get("_error") or plan.get("error") or "")[:1200],
    }
    completion = complete_source_sentences(plan, facts)
    completion["repaginated"] = False
    if already_paginated and (completion["completed_half_sentences"]
                              or completion["added_qualitative"]
                              or completion["dropped_duplicates"]):
        plan.pop("_pagination", None)
        already_paginated = False
        completion["repaginated"] = True
    run["source_completion"] = completion
    plan_problems = revalidate_completed_plan(plan, args.pages, facts)
    run["plan_revalidation"] = {
        "pre_completion_status": pre_completion_status,
        "final_problems": plan_problems,
        "pre_completion_failure_cleared": bool(pre_completion_status["failed"]
                                                and not plan_problems),
    }
    print(f"[complete] 半句补全 {len(completion['completed_half_sentences'])} 条、"
          f"定性补入 {len(completion['added_qualitative'])} 条、"
          f"未放置 {len(completion['unplaced'])} 条、"
          f"重新分页={completion['repaginated']}", flush=True)

    pp = outdir / "content_plan.json"
    pp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    assignments = [t for t in trace if t.get("phase") == "source_assignment"]
    model_call_count = sum(int(t.get("model_calls", 0) or 0) for t in trace)
    run["plan"] = {"path": rel(pp),
                   "mode": mode,
                   "chunk_count": len(assignments),
                   "chunk_calls": 0,
                   "source_assignment_count": len(assignments),
                   "model_call_count": model_call_count,
                   "model_call_scope": "content_plan",
                   "slides": len(plan.get("slides", []) or []),
                   "failed": bool(plan.get("_failed")),
                   "error": plan.get("_error") or plan.get("error"),
                   "trace": trace}
    print(f"[plan] mode={mode} slides={len(plan.get('slides', []) or [])} "
          f"assignments={run['plan']['source_assignment_count']} "
          f"content_plan_model_calls={run['plan']['model_call_count']} "
          f"failed={bool(plan.get('_failed'))}", flush=True)
    if plan.get("_failed"):
        run["verdict"] = "PLAN_FAILED"
        (outdir / "run_record.json").write_text(
            json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[stop] 计划生成失败，保留证据，不产出 PPTX")
        save_run("PLAN_FAILED")
        return 0

    # 取消闸门 1：计划完成后 —— 已取消则不构建、不接纳、不导出
    if cancelled():
        run["cancelled_at"] = "after_plan"
        save_run("CANCELLED")
        print("[cancel] 计划阶段后被取消：未构建 PPTX，未接纳，未导出")
        return 0

    # 规划阶段限制：image_caption 必须绑定受控资产；无资产在此拒绝，
    # 不留到生成期静默降级为文本页。
    asset_problems = validate_plan_assets(plan)
    if asset_problems:
        run["plan_asset_problems"] = asset_problems
        run["verdict"] = "PLAN_ASSET_INVALID"
        save_run("PLAN_ASSET_INVALID")
        print("[stop] 计划声明了无受控资产的 image_caption 页，已在规划阶段拒绝：")
        for p in asset_problems:
            print("        -", p)
        return 0

    # 按真实容量分页（内容完整优先；超量进续页队列，不复制页面凑数）
    if already_paginated:
        # 回放的是**已分页**冻结计划：页数取实际计划，不再二次分页。
        _before = len(plan.get("slides") or [])
        pag = {"pages_before": _before, "pages_after": expected_slide_count(plan),
               "in_range": True, "replayed": True,
               "note": "回放已分页计划：未重新规划、未重新分页。"}
        pp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        run["markdown_table_reclassification"] = reclassify_markdown_table_pages(plan)
        plan, pag = paginate_plan(plan, facts)
        # 与回放路径同一口径：把构建期会前置的封面计入最终页数。
        pag["pages_after"] = expected_slide_count(plan)
        pag["in_range"] = not validate_page_range(pag["pages_after"])
        pp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    run["pagination"] = pag
    print(f"[pages] {pag['pages_before']} → {pag['pages_after']} 页 "
          f"(区间 {PAGE_RANGE[0]}—{PAGE_RANGE[1]}, in_range={pag['in_range']})", flush=True)
    range_problems = validate_page_range(pag["pages_after"])
    if range_problems:
        run["verdict"] = "PAGE_RANGE_UNSATISFIED"
        run["page_range_conflict"] = {
            "pages": pag["pages_after"], "range": list(PAGE_RANGE),
            "problems": range_problems,
            "note": "内容天然不足以落入冻结区间；按契约报告容量/约束冲突，"
                    "不提高容量上限、不复制页面凑数。"}
        save_run("PAGE_RANGE_UNSATISFIED")
        print(f"[stop] 分页后 {pag['pages_after']} 页不满足冻结区间 "
              f"{PAGE_RANGE[0]}—{PAGE_RANGE[1]}；按契约报告冲突，不产出候选。")
        return 0

    # 4) 原生 PPTX
    pptx_path = outdir / f"{mat.stem}.generated.pptx"
    prs = build_pptx(plan, facts, pptx_path, theme_mod)
    # Reopen the saved artifact: the downloadable PPTX, not an in-memory estimate,
    # is authoritative for page count. Also require one ordered manifest row per
    # physical slide before advancing to the visual gate.
    from pptx import Presentation as _Presentation
    object_slides = len(prs.slides._sldIdLst)
    saved_slides = _Presentation(str(pptx_path))
    actual_slides = len(saved_slides.slides._sldIdLst)
    if object_slides != actual_slides:
        run["verdict"] = "PAGE_COUNT_MISMATCH"
        run["page_count_error"] = {
            "in_memory_slides": object_slides,
            "reopened_pptx_slides": actual_slides,
            "note": "saved PPTX differs from the builder's in-memory slide count",
        }
        save_run("PAGE_COUNT_MISMATCH")
        print(f"[stop] 重开 PPTX 为 {actual_slides} 页，内存构建为 {object_slides} 页；"
              "按契约阻断。")
        return 0
    try:
        plan, pag = reconcile_built_page_manifest(
            plan, pag, getattr(prs, "_rendered_page_manifest", None), actual_slides)
    except (RuntimeError, TypeError, ValueError) as exc:
        run["verdict"] = "PAGE_COUNT_MISMATCH"
        run["page_count_error"] = {
            "in_memory_slides": object_slides,
            "reopened_pptx_slides": actual_slides,
            "planner_pages_after": pag.get("pages_after"),
            "detail": str(exc),
        }
        save_run("PAGE_COUNT_MISMATCH")
        print(f"[stop] 实际 PPTX 页数与有序构建清单不一致：{exc}；按契约阻断。")
        return 0

    # 将构建器实际追加的续页写回计划，后续预览、门禁和下载都使用同一页序。
    pp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    run["pagination"] = pag
    run["plan"]["slides"] = len(plan.get("slides") or [])
    run["plan"]["physical_pages"] = actual_slides
    run["pptx"] = {"path": rel(pptx_path),
                   "sha256": sha256_file(pptx_path),
                   "slides": actual_slides,
                   "slides_match_pagination": actual_slides == pag["pages_after"],
                   "matches_reopened_file": True,
                   "matches_builder_manifest": True}
    run["page_count_source"] = (
        "重开保存后的 PPTX 幻灯片数，与有序构建页清单逐页核对")
    if actual_slides != pag["pages_after"]:
        run["verdict"] = "PAGE_COUNT_MISMATCH"
        save_run("PAGE_COUNT_MISMATCH")
        print(f"[stop] 实际页数 {actual_slides} 与分页结果 {pag['pages_after']} 不一致；"
              "按契约报告，不产出候选。")
        return 0
    range_problems_after_build = validate_page_range(actual_slides)
    if range_problems_after_build:
        run["verdict"] = "PAGE_RANGE_UNSATISFIED"
        run["page_range_conflict"] = {
            "pages": actual_slides, "range": list(PAGE_RANGE),
            "problems": range_problems_after_build,
            "note": "构建期续页计入后超出冻结页数区间；不删事实、不缩字号、不导出。",
        }
        save_run("PAGE_RANGE_UNSATISFIED")
        print(f"[stop] 构建实际 {actual_slides} 页，超出冻结区间；按契约阻断。")
        return 0
    # 关系证据落盘：图表逐点的类别/系列/值/单位/来源 fact_id，
    # 以及页型规则（密度/布局/回退/字号）在实际生成入口的调用结果。
    run["chart_relations"] = getattr(prs, "_chart_relations", [])
    run["page_type_decisions"] = getattr(prs, "_page_type_decisions", [])
    print(f"[pptx] {rel(pptx_path)} slides={run['pptx']['slides']} "
          f"chart_points={sum(len(c['points']) for c in run['chart_relations'])}", flush=True)

    # 取消闸门 2：构建完成后 —— 已取消则不接纳、不导出
    if cancelled():
        run["cancelled_at"] = "after_build"
        save_run("CANCELLED")
        print("[cancel] 构建阶段后被取消：候选已生成但不接纳、不导出")
        return 0

    save_run("GENERATED_PENDING_VALIDATION")
    print("[out]", rel(outdir / "run_record.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
