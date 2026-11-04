#!/usr/bin/env python3
"""图表/指标卡标签的**语义抽取**（产品模块）。

问题来源：早先用正则从原句里抓"数字前的那几个字"当标签，实测产出
`同比增长`、`涉及`、`低于`、`个区域共` 这类**句中碎片**，语义不成立；
退化时又落到 `数量（台）` / `数值` / 章节名。

本模块的规则（全部来自冻结事实的原句，不猜）：
1. `{名词}为|是|达到|占 {数字}` → 标签取该名词（如「续约预警覆盖率」）；
2. `涉及|覆盖|包含 {数字} 个|项|台 {名词}` → 标签取该名词（如「存量接口」）；
3. `{数字} 个|台|项 {名词}` → 标签取该名词（如「终端设备」）；
4. 从 X 到/降至/缩短到 Y 的对比 → 两端都用其所属名词；
5. 从 X <单位> <变化动词> Y（缩短到/下降到/提升到…）→ **同一指标**的
   「{指标}·改善前」/「{指标}·改善后」（规则 5，优先于规则 1；
   避免句首名词 + 括号数值冒充可区分类别）；
6. 以上都取不到时，**保留清楚的完整来源描述**（按标点截断的完整短句），
   绝不退化成裸数字、通用章节名或「数值」。

每个标签都带 `fact_id` 与 `source_locator`，供关系核对逐点验证。
"""
from __future__ import annotations

import re

# 出现在名词与数字之间、且**不携带语义**的动词/连接词（只作分隔，不当标签）
_STOP_VERBS = (
    "达到约|达到|占|为|是|共|合计|共计|涉及|覆盖|包含|包括|录得|实现|"
    "提升至|提升到|提升|下降至|下降|降到|降至|减少到|缩短到|增长至|增长到|"
    "同比|环比|较上年|预计|计划|目标|其中|另外|同时|以及|和|与|至|到|"
    "从|由|发生|存在|增长|减少|缩短|压缩"
)
# 注意：必须用非捕获组包住，否则 `|` 优先级会把"动词前缀 + 数值"整个拆成两段，
# 后半段会独立去匹配任意数字（实测把 "74%" 切成 "4%"）。
_NUM = r"(?:[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-+]?\d+(?:\.\d+)?)"

# 通用兜底词：这些**不得**作为标签（是退化信号，不是语义）
_FORBIDDEN_LABELS = {
    "数值", "数量", "指标", "项目", "类别", "其他", "其它", "无", "未知",
}

# 纯计量单位碎片：**不得**当名词标签（实测「4.2 个百分点」被规则 3
# 拆成「个」+「百分点」而抢先在「毛利率为 63.5%」之前命中，产出
# 无意义标签「百分点」）。
_UNIT_ONLY = {
    "百分点", "个百分点", "百分比", "个月", "天", "小时", "分钟", "秒",
    "元", "万元", "亿元", "家", "人", "次", "条", "项", "台", "套", "个",
    "年", "月", "日", "%", "％",
}

# 量词（含复合单位）：`N <计数词> <名词>` 规则中，复合单位词必须整词计入
# 计数词，否则「4.2 个百分点」会把「百分点」当名词抓走。
_COUNTERS = (r"(?:个|台|项|套|条|次|人|家|个百分点|百分点|个月|百分比)")

#: 变化动词：同一指标的「从 A <单位> <动词> B」——两端是同一指标的不同时点。
_CHANGE_VERBS = (r"缩短到|缩短至|下降到|下降至|减少到|降至|降到|提升到|提升至|"
                 r"提高到|增长到|增至|扩大到|扩展至|扩展到")
#: 数值与变化动词之间允许出现的计量单位（「45 分钟缩短到 8 分钟」）。
#: 旧规则 4 只认 _COUNTERS 的计数词，时间/货币单位匹配不上，对比句退化。
_CHANGE_UNITS = (r"分钟|小时|秒|天|个月|年|月|日|元|万元|亿元|个|台|项|次|人|家|%|％")


def _clean(tok: str) -> str:
    t = (tok or "").strip()
    t = re.sub(r"^[的了是为占共]+", "", t)
    t = re.sub(r"[的了是为占共]+$", "", t)
    t = re.sub(r"\s+", "", t)
    return t


def _is_meaningful(label: str) -> bool:
    if not label or label in _FORBIDDEN_LABELS:
        return False
    if label in _UNIT_ONLY:
        return False
    # 以单位词开头的串（「元下」「百分点说明…」）是句中碎片，不是语义名词
    if label[0] in "元万亿" or label.startswith(("百分点", "个百分点", "%", "％")):
        return False
    # 由纯单位/方向字组成的串（「万元」「元下」）不算语义名词
    if label and all(ch in "个万台项套条次人家元角分年月日秒时分百分点下％%" for ch in label):
        return False
    if label.isdigit():
        return False
    # 纯动词/连接词碎片不算语义名词
    if re.fullmatch(r"(同比|环比|涉及|覆盖|包含|其中|另外|同时|以及|预计|"
                    r"计划|目标|达到|占|为|是|共|合计|至|到|和|与)" , label):
        return False
    if len(label) < 2:
        return False
    return True


def _short_source_desc(sentence: str, number: str, limit: int = 22) -> str:
    """取不到可靠短标签时，用**完整但截断到标点边界**的来源描述。

    明确优于「数值」或章节名：至少可核对来源与语义方向。
    """
    s = (sentence or "").strip()
    if not s:
        return ""
    # 取包含该数字的最小完整分句
    idx = s.find(number)
    if idx < 0:
        clause = s
    else:
        left = max(s.rfind(c, 0, idx) for c in "。；！？，,;") + 1
        rights = [s.find(c, idx) for c in "。；！？，,;"]
        rights = [r for r in rights if r >= 0]
        right = min(rights) if rights else len(s)
        clause = s[left:right]
    clause = clause.strip(" 　、，,;；。")
    if len(clause) > limit:
        # 按 docstring 契约截到**标点边界**（2026-11-04 job-3331ea06ac2c 实证）：
        # 早先的实现硬切 limit 字再补 "…"，而保真门
        # （source_fidelity._looks_truncated）把以 "…" 结尾的图表类别一律
        # 判 BLOCKING——兜底标签天然无法过检，等于左手制造缺陷、
        # 右手拦截缺陷。截到标点边界后不得再补省略号。
        cut = limit
        for i in range(min(limit, len(clause)) - 1, 7, -1):
            if clause[i] in "，。、；：！？,;:!?）)】]」』 ":
                cut = i + 1
                break
        clause = clause[:cut].rstrip(" 　、，,;；。:：！?？）)】]」』")
    return clause


def label_for_fact(fact: dict) -> dict:
    """为一个冻结事实推导语义标签。

    返回 {"label", "rule", "fact_id", "source_locator", "reliable"}。
    `reliable=False` 表示只能给出来源描述（进复核，不假装是精确类别）。
    """
    fact_id = fact.get("fact_id")
    raw = str(fact.get("raw_value_text") or "").strip()
    sentence = (fact.get("source_ref") or {}).get("sentence") or ""
    locator = (fact.get("source_ref") or {}).get("locator") or ""
    num_m = re.match(r"[-+]?[\d,]*\.?\d+", raw)
    num = num_m.group(0) if num_m else raw

    label = ""
    rule = ""

    # 规则 2/3：涉及/覆盖 N 个名词  或  N 个名词
    m = re.search(r"(?:涉及|覆盖|包含|包括)\s*" + _NUM
                  + r"\s*" + _COUNTERS + r"?\s*"
                  r"([^，。；、：；\s%]{2,12})", sentence)
    if m:
        cand = _clean(m.group(1))
        if _is_meaningful(cand):
            label, rule = cand, "涉及/覆盖 N 个<名词>"

    # 规则 3：N 个|台|项 名词
    if not label:
        m = re.search(_NUM + r"\s*" + _COUNTERS + r"\s*"
                      r"([^，。；、：\s]{2,12})", sentence)
        if m:
            cand = _clean(m.group(1))
            if _is_meaningful(cand):
                label, rule = cand, "N 个<名词>"

    # 规则 5（置于规则 1 之前，优先级更高）：从 X <单位> <变化动词> Y ——
    # **同一指标**的改善前/后。监督 2026-10-20 实测：P5 图表把
    # 「平均故障定位时间从 45 分钟缩短到 8 分钟」的两个点标成
    # 「智能运维模块·86%的核」与「智能运维模块·86%的核(8)」——规则 1 抢了
    # 句首名词（模块名），批处理消歧又注入异句数字片段，读者无法识别前后。
    # 本规则从变化句直接取指标名 + 本点所处端（前/后），不猜、不截断。
    if not label:
        m = re.search(_NUM + r"\s*(?:" + _CHANGE_UNITS + r")?\s*(?:" + _CHANGE_VERBS
                      + r")\s*" + _NUM, sentence)
        if m:
            seg_nums = re.findall(_NUM, m.group(0))
            own = re.sub(r"[\s,%％]", "", num)
            role = ""
            if seg_nums and own == re.sub(r"[\s,%％]", "", seg_nums[0]):
                role = "改善前"
            elif len(seg_nums) > 1 and own == re.sub(r"[\s,%％]", "", seg_nums[-1]):
                role = "改善后"
            if role:
                head = sentence[:m.start()].strip()
                head = re.sub(r"(?:从|由)\s*$", "", head)
                hm = re.search(r"([^，。；、：\s]{2,14})$", head)
                metric = _clean(hm.group(1)) if hm else ""
                if _is_meaningful(metric):
                    label, rule = f"{metric}·{role}", "对比句（从A到B·改善前/后）"

    # 规则 1：名词 + (为|是|达到|占|达) + 数字
    if not label:
        m = re.search(r"([^，。；、：\s]{2,14}?)\s*(?:" + _STOP_VERBS + r")\s*"
                      + _NUM + r"(?!\d)", sentence)
        if m:
            cand = _clean(m.group(1))
            # 去掉尾部的停用词残留
            cand = re.sub(r"(?:同比|环比|较上年|预计|计划|目标|其中|另外)$", "", cand)
            cand = _clean(cand)
            if _is_meaningful(cand):
                label, rule = cand, "<名词>为/达到 N"

    # 规则 4：从 X 降到/缩短到 Y —— 取较低端所属名词
    if not label:
        m = re.search(_NUM + r"\s*" + _COUNTERS + r"?\s*"
                      r"(?:缩短到|下降到|减少到|降到|降至|提升到|提高到)\s*" + _NUM,
                      sentence)
        if m:
            tail = sentence[m.end():]
            m2 = re.match(r"\s*(?:个|台|项|天|小时|分钟|次)?\s*"
                          r"([^，。；、：\s]{2,12})", tail)
            cand = _clean(m2.group(1)) if m2 else ""
            head = sentence[:m.start()]
            hm = re.search(r"([^，。；、：\s]{2,12})\s*$", head)
            cand2 = _clean(hm.group(1)) if hm else ""
            pick = cand or cand2
            if _is_meaningful(pick):
                label, rule = pick, "对比句（取变化对象）"

    if _is_meaningful(label):
        return {"label": label, "rule": rule, "fact_id": fact_id,
                "source_locator": locator, "reliable": True}

    # 兜底：完整来源描述（明确标记为待复核，不假装精确类别）
    desc = _short_source_desc(sentence, num)
    return {"label": desc or raw, "rule": "完整来源描述（可靠短标签不可得）",
            "fact_id": fact_id, "source_locator": locator, "reliable": False}


def labels_for_facts(facts: list[dict]) -> list[dict]:
    """批量抽取。同一指标名下的**不同取值**必须可区分。

    实测问题：同一句里的「覆盖率为 74%，低于 85% 的目标」两条事实都指向
    「续约预警覆盖率」，加序号能区分但不说明差异。
    这里改为在重复时补上**该事实自身的限定**（目标/当前、同比/绝对等），
    取不到就追加原句中的数值后缀，仍不以裸数字充当类别。
    """
    out = []
    used: dict[str, int] = {}
    for f in facts:
        r = label_for_fact(f)
        base = r["label"]
        if base in used:
            used[base] += 1
            r["label"] = _disambiguate(f, base, used[base])
            r["rule"] += " + 重复消歧"
        else:
            used[base] = 1
        out.append(r)
    return out


def _disambiguate(fact: dict, base: str, nth: int) -> str:
    """同名的第 n 个取值：用**本点自身**的限定词或自身数值区分。

    监督 2026-10-20 纠偏：不得把句中**其他点**的数值拼成后缀——
    「低于 85% 的目标」这条事实曾被消歧成「…·74%」（74% 是同句另一点的
    当前值），属于把 A 的值配到 B 的类别上。锚点只能是本点自己的
    raw_value_text：取其后的限定词（目标/当前/同比…），取不到才用自身数值，
    仍取不到用序号。绝不用异句/异点数字。
    """
    sent = (fact.get("source_ref") or {}).get("sentence") or ""
    raw = str(fact.get("raw_value_text") or "").strip()
    if raw and raw in sent:
        tail = sent[sent.find(raw) + len(raw):]
        m = re.match(r"\s*(?:%|％|个百分点|元|万元|亿元|分钟|小时|秒|天|个|台|项|次|人|家)?\s*"
                     r"([^，。；、：\s]{0,6})", tail)
        qual = _clean((m.group(1) if m else "") or "")
        for w in ("目标", "当前", "同比", "环比", "计划", "预计", "承诺", "预算",
                  "上限", "下限", "均值", "中位数"):
            if w in qual:
                return f"{base}·{w}"
        own = re.sub(r"[\s,%％]", "", raw)
        if own and own not in re.sub(r"[\s,%％]", "", base):
            return f"{base}·{raw}"
    return f"{base}{nth}"


if __name__ == "__main__":
    import json
    demo = [
        {"fact_id": "F1", "raw_value_text": "74%",
         "source_ref": {"locator": "p2/l3",
                        "sentence": "客户成功团队人手不足，续约预警覆盖率为 74%，低于 85% 的目标"}},
        {"fact_id": "F2", "raw_value_text": "22 个",
         "source_ref": {"locator": "p3/l1",
                        "sentence": "老版本接口未完全下线，维护成本较高，涉及 22 个存量接口"}},
        {"fact_id": "F3", "raw_value_text": "218 台",
         "source_ref": {"locator": "p4/l2",
                        "sentence": "本次审计覆盖公司及下属 3 个区域共 46 台服务器、218 台终端设备和 12 个业务系统"}},
        {"fact_id": "F4", "raw_value_text": "8 分钟",
         "source_ref": {"locator": "p1/l4",
                        "sentence": "智能运维模块覆盖 86% 的核心告警场景，平均故障定位时间从 45 分钟缩短到 8 分钟"}},
        {"fact_id": "F5", "raw_value_text": "3",
         "source_ref": {"locator": "p5/l2",
                        "sentence": "本次审计覆盖公司及下属 3 个区域共 46 台服务器、218 台终端设备和 12 个业务系统"}},
        {"fact_id": "F6", "raw_value_text": "97.5%",
         "source_ref": {"locator": "p9/l1",
                        "sentence": "作为首要改进方向，并与准时送达率 97.5% 的目标绑定考核"}},
    ]
    print(json.dumps(labels_for_facts(demo), ensure_ascii=False, indent=2))