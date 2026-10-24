# adaptive P2 目录两栏错位修复报告（2026-10-20 19:25 +0800）

> 触发：监督 19:08–19:10 独立复核（`material_layout_replay_20261020_1908/`）。
> 监督确认两处原可见性缺陷（visual-dark 目录裁切、P5 来源说明被遮挡）已修复；
> 唯一剩余缺陷：wengao-adaptive P2 并排目录两栏错位（旧导出件 SHA256
> `48eaccddb102db1669989b177875511c8700b7cb7fe3b90ae30cae9d748ea53d`）。

## 缺陷实测（监督证据 + 本地复测一致）

- 左栏文本框 top=4.04in、右栏 top=1.90in，相差 2.14in；
- 左栏框体底部 = 4.04+5.10 = 9.14in，超出 7.5in 画布；
- 顶锚定使 8 条字形暂时完整可见，但视觉质量未过关（并排目录不对齐）。

## 根因（实验定位，非推测）

`refine_deck` 的 `rule_universal`（vcenter：盒内唯一单行文字垂直居中）误伤：

1. 材料生成 `add_bullets` 双栏分支用 `slide_layouts[1]` 建页（自带 body 占位符），
   双栏文字放进两个新建 textbox 后，**空的 body 占位符残留在页面上**；
2. `refine.Sh.is_text` 要求 `text_frame.text.strip()` 非空 → 空占位符
   `is_text=False` → 落入规则里的 "cards" 列表（W>0.8、H>0.35 满足）；
3. vcenter 把左栏 TextBox 3 判定为该空卡内"唯一单行文字"（line_count 按卡宽
   11.9in 估算为 1 行），垂直居中进 1.9→7.0in 的空框 → 左栏被推到 4.04in。

本地复现实验：`refine_deck(修复前生成稿)` → P2 actions =
`[{'rule': 'vcenter', 'shape': 'TextBox 3', 'card': 'Content Placeholder 2'}]`，
机制完全吻合。

## 修复（只改材料生成模块；refine.py 零改动 → 原稿导入路径零影响）

`samples/r5_material_to_ppt.py` `add_bullets` 双栏分支：显式删除页面上的
**非标题占位符**（`placeholder_format.idx != 0`）。该占位符无任何文字，
删除不丢事实、不影响单栏路径；layout 标题占位符保留（标题照常）。

配套门禁升级 `scripts/geometry_gate.py`（监督要求"覆盖两栏对齐和框体边界"）：

- **A 级 PPTX 框体几何**（新增）：全部形状 bottom/right 不超画布；
  并排文本框两栏同顶同高（top 差 ≤0.05in）；
- **B 级 PDF 字形几何**（保留）：文本画布内、两两不重叠、目录 8 条可见、
  内容条款、页数 10–20。

## 验证

**反向验证（先证门禁有效）**：对修复前旧导出件跑新门禁 → adaptive 被精确
抓住（`[框体] P2 框体出界: 'TextBox 3' L0.70 T4.04 R6.55 B9.14`、
`[框体] P2 两栏未对齐: T4.04/H5.10 × T1.90/H5.10`），其余 5 主题 PASS
（与监督复核结论一致）。

**六主题重导（确定性回放，零模型调用）+ 全门禁**：6/6 PASS。

- adaptive P2 实测：两栏 **同顶 T=1.90in**、同宽 5.85in、同高 5.10in、
  底部 7.00in（画布内），8 条 4+4，字号 22pt；目视复核渲染图确认两栏
  严格对齐、无错位；
- 已通过项全部保持：六主题 12 页、三条原句 + "客户成功团队人手不足"保留、
  图表类别"平均故障定位时间·改善前/改善后"（45/8、0 起点）、正文 22、
  表格 18–20、辅助 16、标题 36、无 <16pt；
- `source.fact_coverage=EXECUTED`、findings=0、verdict=DEVELOPMENT_CANDIDATE（六任务）。

**新导出件 SHA256**：

| 主题 | SHA256 |
|---|---|
| wengao-basic | `afdfc3dce0c0a0566e0ce14a32e81028e24533e5256b429dc42cef3ae3b94545` |
| wengao-business | `548471a4f2c277ed5dc56667500b38534e0b9e90c6ac30b4a5106c6960ebcace` |
| wengao-minimal | `67c650b1b5f5efa342609e7871847bea5b659e637a3df52f74174f334e3caf50` |
| wengao-dark | `1f3d66c046b80645759032cb2e0d78ba0b26a5f51ad08bd9a61065330751e714` |
| wengao-visual-dark | `5876e8c77048abb5f7fb683500a6f4a37e6c51a893e361ab04e6d431bcf6732b` |
| wengao-adaptive | `2c7df3cbaa15619c3ff2dfa339a7d43ef0a3b2913917eaea9c22a81ada443023` |

（旧缺陷件 basic 上轮 `2b4b5d9e…`/adaptive `48eaccdd…` 等已被本轮取代。）

## 边界

- 8821/v12 未动（只读探活 HTTP 200）；源材料 sha `659255db…` 未变；
  exact 原稿 sha `e43dad9e…` 未变；
- 未做任何 UI 自动化/录屏/发布；isolated 候选为唯一写者。

## 新冻结身份

- **build_id `481ddf41892d8692ce92d831d573ef7d`**（development 实例，
  数据根 `wb_loopback_data` 隔离），入口 `http://127.0.0.1:8823/`；
  连续多次只读 `/api/meta` 身份一致；旧身份 `317d4e08…`（19:03 轮）
  与 `3e4682cc…`（18:10 轮）留档对照，不套用。
- 本轮改动程序文件：`samples/r5_material_to_ppt.py`
  （sha `d9c1957ddc652a62…`）、`scripts/geometry_gate.py`
  （sha `1f71329eba65e6ee…`）。

## 仍未完成（不宣布总体完成）

1. 监督方新上传→真实新模型→预览→下载/字号/内容/图表/排版 UI 全链复验（Cua，8823）；
2. 原生 exact 实播 GUI 复验（经 `scripts/open_native.sh`）与 30–60s 连续录像；
3. 干净安装（独立根）首任务；
4. GitHub/SkillHub 实际发布（授权闸门：全部真实验收通过后；当前未达）。
