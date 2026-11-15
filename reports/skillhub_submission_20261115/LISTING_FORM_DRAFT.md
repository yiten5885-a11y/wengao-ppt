# 稳稿 PPT · 上架表单草稿（可直接复制粘贴）

> 我没有"WorkBuddy 开放平台"实际提交表单的访问权限（没有对应工具，且账号登录/
> 实名这些步骤规则上必须你本人做）。下面按常见的技能/插件市场表单字段整理好了
> 每一项的现成文案，你打开真正的提交页面后照抄即可；如果实际表单的字段名、字数
> 限制、必填项跟下面不一样（比如多一个"演示视频"或分类选项是下拉菜单而不是自由
> 填写），把那个页面截图或字段列表发我，我再按实际表单调整映射。

---

## 1. 基本信息

| 字段 | 填写内容 |
| --- | --- |
| 产品名称 / 显示名 | 稳稿 PPT（WenGao PPT） |
| 标识符 / slug | `wengao-ppt` |
| 版本号 | `0.1.0`（内部标注 `0.1.0-dev-candidate`，如果表单允许备注"开发候选"就带上，不允许就填 `0.1.0`） |
| 分类 | 办公效率（如果平台分类是英文下拉，选 Office Productivity / Productivity） |
| 标签 | PPTX、演示文稿、本地模型、独立验收、多语言、场景配图、中文排版 |
| 许可证 | MIT |
| 项目主页 / 代码仓库 | https://github.com/yiten5885-a11y/wengao-ppt |
| 语言 | 简体中文（界面与文档），命令行工具本身对输入语言无限制 |

---

## 2. 一句话简介（headline / summary，一般 50—80 字以内）

```
本地优先的 PPTX 生成与美化引擎；产物必须经独立验收器重读落盘文件才算数，生成器自报一律不采信。
```

英文版（如表单需要）：

```
A local-first PPTX generation and beautification engine — every verdict is produced by an independent verifier that re-reads what actually landed on disk, never the generator's own self-report.
```

---

## 3. 详细描述（长描述，中文）

```
稳稿 PPT 有两条使用路径：

① 美化已有 PPTX —— 把一份排版凌乱的 PPTX 整理成结构规范、字号统一、配色协调
的成品，不改动你写的内容。支持纯美化零配置模式：只需本机装好 LibreOffice，
不用配任何 AI 模型就能真实跑完"预检→生成→验收→发布→导出"全流程。

② 由原始材料生成 PPTX —— 把一份 Markdown/TXT/Word 长文档，自动拆解成结构
清晰、事实不丢字、配图有来源且合规的原生 PPTX。这条路径需要你自备一个模型
后端（本机 llama-server/Ollama，或你自己的云端 API Key）。

核心差异化：所有"做完了"的结论都由一个独立验收器重新读取磁盘上实际生成的
PPTX、PDF、渲染像素和字形轮廓来判定，生成器自己说"完成了"不算数——不会把
半成品静默标成可交付。六套主题（含一套"保留原稿设计、只修结构"的适配主题）
都经过 WCAG 对比度实算；提供命令行与本机网页工作台两种操作入口；支持对已
生成内容做局部修改（文字/表格/图表/图片）并保留历史版本可回滚。

适合谁：需要批量产出述职汇报、经营评审、投标演示、培训课件，又不想每次都
手动调版式、对字号、挑配色的个人或团队。

重要提示（请买前确认）：当前交付状态为"开发候选版"，不是完整产品
READY——26 种非中文语种的真实端到端生成、50 套完整验收用例尚未全部跑完；
由原始材料生成以及语义场景配图这两项功能仍需要你自备模型后端，只有"美化
已有 PPTX"支持零配置使用。已知限制详见随包说明与 KNOWN_ISSUES。
```

### 英文版

```
WenGao PPT offers two workflows:

1) Beautify an existing PPTX — turn a messy deck into one with consistent
structure, type scale and color, without touching your actual content. Plain
beautify now supports a zero-config mode: with LibreOffice installed and no
AI model configured at all, you can run the full
precheck -> run -> validate -> publish -> export pipeline end to end.

2) Generate a PPTX from raw material — turn a long Markdown/TXT/Word
document into a structurally sound, fact-preserving, properly-licensed
native PPTX. This path needs you to bring your own model backend (a local
llama-server/Ollama, or your own cloud API key).

What makes it different: every "done" verdict comes from an independent
verifier that re-reads the actual PPTX/PDF/rendered-pixel/glyph-outline
bytes on disk — the generator's own self-report is never trusted, so a
half-finished result is never silently marked ready. Six themes (including
one that preserves the source deck's own design language and only fixes
structure) are checked against WCAG contrast math; both a CLI and a local
web workbench are available; targeted edits (text/table/chart/image) keep a
rollback-able history.

Who it's for: individuals or teams who need to batch-produce status
reports, business reviews, pitch decks, or training materials without
manually re-tuning layout, font sizes and colors every time.

Please confirm before buying: current delivery status is a development
candidate, not a finished READY product — real end-to-end generation for
the 26 non-Chinese target languages and the full 50-deck acceptance suite
are not fully run yet. Only plain beautify supports zero-config use;
material generation and semantic scene photos still need your own model
backend. See the bundled docs and KNOWN_ISSUES for details.
```

---

## 4. 系统要求 / 运行环境（如表单有单独字段）

```
- Python 3.10+（买家自行 pip install -r requirements.txt）
- 本机 LibreOffice（真实渲染/导出依赖，必装）
- 可选：本机模型后端（llama-server / Ollama）或云端模型 API Key
  —— 仅"由原始材料生成 PPTX"和"语义场景配图"这两项功能需要；
     纯美化（--skip-scene-photos）不需要
- 操作系统：开发与测试均在 macOS 上完成；Linux 理论兼容（依赖 LibreOffice
  与 Python 生态，未在 Linux 上做过正式验证）
```

---

## 5. 定价（来自 PRICING_PROPOSAL.md，已按你的决策更新）

| 档位 | 价格（一次性） | 说明 |
| --- | --- | --- |
| 个人版 | ¥149 | 单机使用，含全部主题与材料生成功能；纯美化零配置可用 |
| 团队版（≤5 人/同一组织） | ¥499 | 个人版全部能力 + 团队多人安装；附一次远程答疑 |
| 源码可维护授权 | ¥999 | 附带可读写源码，便于二次开发 |

如果表单只允许填一个价格（没有分档功能），先填 **个人版 ¥149**，团队版/源码版
等平台支持多 SKU 或你手动走私下授权时再补。

---

## 6. 封面图 / 截图

**不要用** `assets/visual_img/` 下的示例配图（AI 生成图与 Pexels 图许可未理清，
且安装包本就不含它们）。改用项目自己真实渲染产出的页面截图，例如：

```
reports/themes-20261105-v2/final-page-01.png
reports/themes-20261105-v2/final-page-03.png
reports/themes-20261105-v2/before-page-01.png（美化前，可做"前后对比"素材）
```

这些都是本项目自己生成的产物，没有第三方版权问题。如果表单要求固定尺寸
（比如 16:9 或正方形），告诉我具体尺寸，我可以帮你从渲染产物里挑/裁一张。

---

## 7. 已知限制 / 免责声明（建议放在商品页显著位置，不要藏在详情里）

```
- 当前为开发候选版（DEVELOPMENT_CANDIDATE），不是完整产品 READY。
- 由原始材料生成 PPTX、语义场景配图：需要你自备模型后端（本机或云端）。
- 26 种非中文语种的真实端到端生成、50 套完整验收用例尚未全部跑完。
- 安装包不含示例配图素材（许可限制），首次运行不会自带内置照片。
```

---

## 8. 更新日志（首次上架可以只写一条）

```
0.1.0（2026-11-15）首次上架候选
- 美化已有 PPTX：支持零配置模式（--skip-scene-photos），只需 LibreOffice
- 由原始材料生成 PPTX：md/txt/docx 输入，含事实台账核对与多语言规划
- 六套主题，WCAG 对比度实算；命令行 + 本机网页工作台双入口
- 局部修改（文字/表格/图表/图片）与历史版本回滚
```

---

## 下一步

把真实提交页面的字段截图发我（或者直接告诉我字段名和字数限制），我按实际表单
重新核对一遍映射，确保每个空都填对内容、不超字数。账号登录、实名认证、收款
信息这几步还是要你自己在那个页面上操作。
