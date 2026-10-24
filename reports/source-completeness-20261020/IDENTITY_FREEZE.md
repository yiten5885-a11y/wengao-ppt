# 候选源码身份冻结（IDENTITY_FREEZE，2026-10-20 18:10 +0800）

> **⚠️ 19:27 更新（当前权威，上一轮 `317d4e08…` 已被取代、仅留档）**：
> 监督 19:08–19:10 独立复核确认两处原可见性缺陷已修复，唯一剩余：
> wengao-adaptive P2 并排目录两栏错位 2.14in（左栏 T=4.04in、框体底部
> 9.14in 出画布；旧导出件 `48eaccdd…`）。根因：双栏分支遗留的空 body
> 占位符被 adaptive refine 的 vcenter 规则误判为"卡片"，把左栏文字
> 垂直居中进空框。已修复（只删材料生成双栏分支的空占位符，refine.py
> 零改动、原稿导入路径零影响）并升级几何门禁（框体边界 + 两栏对齐）。
> **新 build_id = `481ddf41892d8692ce92d831d573ef7d`**（详见同目录
> `adaptive目录对齐修复报告.md`）。build `317d4e08c9fb43e4011bdd9b01906169`
> （19:10 排版轮）与 `3e4682cc45d8981cecda9dc95023e452`（18:10 内容轮）
> 保留用于对照，**不得再套用在改后代码上**。
> 下方正文为 18:10 轮的原始记录，原样保留。

> 用途：监督方下一轮 UI 验收（新上传 → 真实新模型 → 预览 → 下载/字号/内容/图表）
> 使用的源码/build 身份。**身份冻结后不再改动程序成员**；后续只写 reports/
> （已被 build_id 排除）与产品根的交接文档（不属本程序树）。

## 1. 冻结身份

| 项 | 值 |
|---|---|
| 实例 | `http://127.0.0.1:8823/`（回环，development 实例） |
| 程序目录 | `build/wengao-visual-v13-material-fix`（工程 `/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt` 内） |
| **build_id** | **`3e4682cc45d8981cecda9dc95023e452`** |
| 数据根 | 同树 `wb_loopback_data/`（隔离；与 8821 数据互不影响；8821 未动） |
| 启动方式 | `WENGAO_DATA_ROOT=<prog>/wb_loopback_data python workbench/server.py --port 8823` |
| 实例自述 | kind=development / label=wengao-visual-v13-material-fix / install_record=None |

**稳定性判据（监督复现口径）**：连续多次只读 `GET /api/meta` 的 build_id 均为
`3e4682cc…`（本次冻结前后各复核 3 次，含 server.log 追加场景，全部一致）。
旧 v30r4-design 的 INSTALL_RECORD.json 已清理（备份 `/tmp/stale_install_record_v30r4_backup.json`），
开发候选不再被误判为安装实例、不继承旧身份。

## 2. 本轮改动的程序成员（sha256 前 16 位，冻结时点）

```
d5f95f847e5f022a  wengao_ppt/theme.py            （材料路径可读性下限 MATERIAL_SIZE_FLOOR）
e0b68b01b58236f5  wengao_ppt/runner.py           （材料任务传 size_floor）
ba630ff778023928  wengao_ppt/install.py          （build_id 排除数据根/日志等运行期成员）
b2644496ce255dcf  wengao_ppt/label_extract.py    （规则5：从A到B → 同指标改善前/后；消歧锚定本点）
70a868cf3841a36b  wengao_ppt/source_fidelity.py  （定性必讲句覆盖门禁 + 半句门禁）
46e06b4fd6282860  workbench/server.py            （实例判定改依安装布局；记录不匹配标 MISMATCH）
2ca6e0f23057605a  samples/r5_material_to_ppt.py  （跨行完整句+定性事实；确定性补全；图表去括号数值）
3020cc6d6b0d2c21  scripts/open_native.sh         （新增：原生打开字体兼容启动器）
b5569d773d338561  LICENSE-STATUS.md              （Lucide 引用按实际位置修正）
46f664422610b240  requirements.txt               （记录已验证依赖版本与可复现安装说明）
```

已删除：候选树内遗留的 `INSTALL_RECORD.json`（v30r4-design 旧安装记录）。

## 3. 冻结时点的验证结论（证据见同目录）

- 内容：P12 半句补全 2 条、定性必讲句补入 2 条（年内完成三大能力建设 / 主要短板在
  跨区域数据同步延迟与老接口维护成本）、无半句残留；12 页（10–20 区间内）。
- 图表：P5 类别 = `平均故障定位时间·改善前/改善后`（值 45/8，单位分钟，0 起轴）。
- 字号（回放稿 × 六主题全链 export 实测）：basic/business/minimal/dark 正文 22、
  表格 18、标题 36、图表 16；visual-dark 正文 30（其视觉引擎标题角色，容量校验通过、
  渲染无溢出）；adaptive 正文 22。15pt 回归消除。
- 门禁：六主题 `source.fact_coverage=EXECUTED`、0 来源侧 finding、verdict
  DEVELOPMENT_CANDIDATE / release_ready=false。
- 反向验证：新台账 × 旧缺陷稿（e44e11df）→ 5 条 BLOCKING（图表类别错绑 ×2、
  定性缺失 ×2、P12 半句 ×1），三类监督报告的缺陷均可被门禁捕获。
- 原生缺字：诊断 + `scripts/open_native.sh` 启动兼容路径（无头机制实测通过；
  GUI 画面复核归监督方 Cua，见 `原生缺字诊断.md`）。

## 4. 给监督方的下一步（UI 验收）

1. 用 Cua 在 `http://127.0.0.1:8823/` 走**新上传 → 真实新模型 → 预览 → 实际下载**；
2. 核对下载件：字号（正文 22–24/表格 18–20/辅助 ≥16/标题 32–40）、P12 完整句、
   P5 图表前后类别、页数 10–20；
3. 原生复核可用 `scripts/open_native.sh` 打开 exact v12（环境已注入；SHA 不变）；
4. 验收通过前不发布；本冻结身份即发布清单指向的身份（不钉 v12）。
