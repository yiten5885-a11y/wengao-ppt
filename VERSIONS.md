# 版本变更记录 (VERSIONS.md)

## 2026-10-27: WG-LAYOUT-20261027.1 / WG-TYPE-20261027.1

- **任务标识**：WENGAO-TYPE-SCALE-10-30
- **变更性质**：原稿字号体系从"一律 ≥24pt"升级为"10–30pt 分级 + 按页适配"
- **变更原因**：
  现行规则（2026-10-21 "字太小"要求）强制所有正文至少 24pt、标题 36–40、关键数字 44–56，原稿小字一律抬到下限。对于按 10–14pt 设计的高密度卡片原稿（如《从卖流量到卖Token 副本》第 3、4、6 页），24pt 文本无法容纳，导致 579 条排版门禁发现（包括 87 条 OWNER_BOX_OVERFLOW、330 条 HG02 字符缺失/对不上、19 条 REGION_INTRUSION）。新体系在 10–30pt 范围内按角色分级，按每页实际空间选取能放下的最大档位，保持层级清晰，解决拥挤溢出与出页问题。
- **旧规则**：
  - policy_version: `WG-LAYOUT-20261021.1`
  - policy_document_sha256: `3a1e4498bf2b5ec49bce8e6c1042a7625454de561607c47c548096c840645d96`
  - policy_bundle_sha256: `0009870f902fe7c4035ab5b74c8dd3f3b08b63e33c9fbb786e7de07ea613a809`
  - font_roles_pt: title [36, 40], body_hard_min 24, conclusion [28, 32], key_number [44, 56], table [20, 22], chart_aux [18, 20], ordinal [18, 22], footer [16, 18]
- **新规则**：
  - policy_version: `WG-LAYOUT-20261027.1`
  - type_scale_version: `WG-TYPE-20261027.1`
  - policy_document_sha256: `cb18f4ec7f88b195e0d7cd74f50c5abaad3f0f27009310c647ef3a35f002094c`
  - policy_bundle_sha256: `dfb9f3289e9aacafe00088f55a2b9c567e02332a4555eae2f00edb1774899974`
  - font_roles_pt: title [24, 30], lead/conclusion [16, 22], body [13, 18], table/label [11, 14], chart_aux [10, 12], ordinal [12, 16], footer/source [10, 11]
  - 装饰性大字（≥60pt 单字/1–2 字符）归为 `decor`，单独计数并不受 30pt 上限约束。

## 2026-10-29: 图表图例角色（门禁实现变更，政策文件不变）

- **变更性质**：角色判定新增"图表图例"规则，产品（`wengao_ppt/refine.py:infer_text_roles`）与门禁（`scripts/layout_gate.py:infer_roles`）共用 `wengao_ppt/type_scale.py:is_chart_legend_label`，口径一致。
- **变更原因**：job-3afdd5d88bd9（深色视觉升级主题）第 6 页阶梯色条图旁的短图例（"能力分级 L1 — L5""L5 引领级"等）被判为正文，抬到正文下限 13pt 后行高变大、伸进下一根色条，产生 5 条 HG02 CHAR_PARTIAL_VISIBLE + 3 条 HG04 LATER_SHAPE_OCCLUSION。用户 2026-10-29 决定按图表标注（chart_aux，10–12pt）处理。
- **规则**：≤16 字单行文本，与一列（≥2 根、左边对齐 ±0.05in）实底小色条（高 0.08–0.35in）处于同一水平带，且与其中一根重叠或位于其右侧 ≤0.35in；该列色条宽度差 ≥0.10in（条形图以长度编码数值）。等宽标签底片、细分隔线/强调线不算。
- **未改动**：`contracts/layout_hard_gate_policy.json`（policy_version 仍为 `WG-LAYOUT-20261027.1`，bundle/document 哈希不变）；各角色字号区间不变。
- **门禁源文件哈希**（每次运行记录在 `layout_gate_source_sha256`，无冻结比对）：`scripts/layout_gate.py` = `c8086de3625e2d6ae85f2045cfc8ee8ebcb8fce195be1a6b8f32ba4ee699d4e4`；`wengao_ppt/type_scale.py` = `2e8e2a9c3798c4b2b7719e14d848629534633bf728f34124a46b8692b2c60801`。
- **测试**：`tests/test_chart_legend_role.py`（正例：阶梯色条；反例：等宽标签底片、细线/单根强调条、长文本；门禁与产品引用同一函数）。
