# RS-1 基线重跑报告（模板 + 阻塞说明）

日期：2026-10-24　负责人：W4　任务书条目：RS-1

## 状态：⏳ 待 Mac mini 本机执行

**阻塞原因（如实报告，不自行绕过）**：本交付机为 MacBook Pro（`yitenhuadeMacBook-Pro.local`），
Mac mini 通过 SMB 挂载为只读用途的开发源。RS-1 要求的两个本机条件在本机均不满足：

1. **真实 Qwen**：Mac mini 的 Ollama（127.0.0.1:11434）从本机跨网不可达
   （Bonjour `.local` 解析被网络策略阻止，实测 `curl http://…local:11434/api/version` 返回 000）。
2. **LibreOffice**：本机未安装（`/Applications/LibreOffice.app` 不存在）。
   按"不下载未批准的大模型/大软件"的边界，未在本机安装 LibreOffice。

且任务书 §2.3 明确"所有开发与验证会话直接在 Mac mini 本机运行，不再通过 SMB 挂载远程操作"。
因此 RS-1 的**执行**必须在 Mac mini 本机完成；W4 已交付可直接执行的脚本与报告骨架。

## 执行方式（Mac mini 本机，一条命令）

```bash
cd /Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix
# 先应用 W4 交付包（APPLY.md），然后：
python scripts/rs1_baseline.py --input <13页原稿.pptx>
```

脚本动作：校验输入 sha256（任务书记载 `be8614d9…4065f`）→ 真实 CLI `precheck → run → validate`
（mode=beautify，逐阶段计时）→ 对候选 PPTX 用 LibreOffice+pdfium 逐页渲染到
`reports/rs-w4/rs1-baseline/pages/` → 写 `run.json`。

## 人工看图清单（执行后由 W4 填写，不得由脚本代填）

| 页 | 角色/内容 | 渲染是否正常 | 有无明显问题（文字溢出/缺字/错图/版式塌陷） | 备注 |
|---|---|---|---|---|
| 1 | 封面 | | | |
| 2 | | | | |
| … | | | | |
| 13 | 结尾 | | | |

## 已知预期失败（来自任务书 §1 证据，用于对照"改动前"状态）

- 17/17 场景无图 → `VISUAL.SCENE_UNRESOLVED` → 整稿 FAILED、导出 0 文件（真实任务 job-167a1306f549 的既有结论）。
- RS-2 完成后同一稿件重跑必须出稿；本报告是该"改动前"对照。

## 各阶段耗时与失败码

见 `run.json`（执行后生成）。若出现新的非取图阻塞，如实列出，不掩盖。
