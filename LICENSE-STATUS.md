# 稳稿 PPT · 第三方组件许可与资产来源审查台账

> **⚠️ 已被取代（2026-10-24）**：本文件是 2026-10-22 的早期台账，**保留作历史记录**，
> 不再作为对外口径。其中至少三处已与实测不一致：① Pillow 许可当时写 HPND，PyPI 现行
> SPDX 为 `MIT-CMU`；② 表中列的两张 Wikimedia 图（`Radiology_in_Afghanistan…`、
> `Assembly_worker_Philips_Hamilton.jpg`）在本候选 `assets/` 内**已不存在**；
> ③ Lucide 许可文件路径当时写作 `vendor/luobo-ppt/LICENSE-lucide.txt`，实际在
> `vendor/luobo-ppt/assets/LICENSE-lucide.txt`。
>
> **现行口径请一律以 `THIRD-PARTY-NOTICES.md` 与
> `reports/p2-license-release-20261023/LICENSE-MATRIX.md` 为准**（后者带版本与 SHA，
> 由 `scripts/p2_build_license_matrix.py` 现场实测生成，可重跑复现）。

> **严正声明**：本候选包不代替用户接受任何商业或专有法律条款；自有代码最终许可证留待用户亲自决定与签署。公开发布前，本审查台账作为合规前置凭据保存。

---

## 一、第三方开源组件与依赖

| 组件名称 | 来源位置 | 许可协议 | 许可文件路径 | 审查结论与限制 |
| :--- | :--- | :--- | :--- | :--- |
| **luobo-ppt** | `vendor/luobo-ppt/` | **MIT License** | `vendor/luobo-ppt/LICENSE` | 允许商用、修改与再分发，完整版权与许可声明必须在发行包中保留。 |
| **Lucide Icons** | `vendor/luobo-ppt/` | **ISC License** | `vendor/luobo-ppt/LICENSE-lucide.txt` | 允许商用与分发，必须随图库保留 ISC 版权声明。 |
| **python-pptx** | 依赖项（PyPI） | **MIT License** | `requirements.txt` | 允许标准集成与打包分发。 |
| **Pillow (PIL)** | 依赖项（PyPI） | **HPND License** | `requirements.txt` | 允许开源与商业分发。 |
| **PyPDF / pypdfium2** | 依赖项（PyPI） | **BSD-3-Clause / Apache 2.0** | `requirements.txt` | 渲染分析依赖，允许集成分发。 |

---

## 二、语义场景配图（Semantic Scene Photos）资产审查

开发候选包中 `assets/visual_img/semantic_scenes/` 内置的 5 张示例摄影图片均经过来源与许可复核：

| 资产文件名 | 场景标识（Scene ID） | 创作者 | 许可类型 | 来源页面 URL | 分发审查状态 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `consumer-shopping-4971925.jpeg` | `consumer.supermarket_shopping` | Gustavo Fring | Pexels License | `https://www.pexels.com/photo/woman-shopping-in-supermarket-4971925/` | `NOT_CLEARED_FOR_SOURCE_BUNDLE`（仅开发候选包有效；禁止单独打包为图库转售） |
| `consumer-payment-receipt-3570244.jpeg` | `consumer.receipt_settlement` | Hook Tell | Pexels License | `https://www.pexels.com/photo/photo-of-a-person-printing-a-receipt-3570244/` | `NOT_CLEARED_FOR_SOURCE_BUNDLE` |
| `factory-workers-32048366.jpeg` | `production.factory_workers` | EqualStock India | Pexels License | `https://www.pexels.com/photo/textile-factory-workers-in-a-production-line-32048366/` | `NOT_CLEARED_FOR_SOURCE_BUNDLE` |
| `production-line-modern-34718930.jpeg` | `production.capacity_line` | Yetkin Ağaç | Pexels License | `https://www.pexels.com/photo/industrial-factory-interior-with-conveyor-34718930/` | `NOT_CLEARED_FOR_SOURCE_BUNDLE` |
| `delivery-courier-6868460.jpeg` | `delivery.food_courier` | Pexels contributor | Pexels License | `https://www.pexels.com/photo/man-in-red-t-shirt-riding-bicycle-on-road-6868460/` | `NOT_CLEARED_FOR_SOURCE_BUNDLE` |
| `Radiology_in_Afghanistan_141008-N-JY715-001.jpg` | `medical.radiologist_ct_scan` | Lt.Cmdr. Jesse Ehrenfeld (US Navy) | Public domain | `https://commons.wikimedia.org/wiki/File:Radiology_in_Afghanistan%2C_Imaging_the_wounded_141008-N-JY715-001.jpg` | `PUBLIC_DOMAIN_CLEARED`（公有领域无版权限制，用于智慧医疗合成演示） |
| `Assembly_worker_Philips_Hamilton.jpg` | `production.factory_assembly_worker` | Scottish Government | CC0 1.0 Universal | `https://commons.wikimedia.org/wiki/File:Assembly_worker,_Philips,_Hamilton,_Scotland.jpg` | `PUBLIC_DOMAIN_CLEARED`（CC0 公有领域豁免，用于工业制造场景演示） |

> **图库版权分发规则**：Pexels 协议允许将图片用于终端演示文稿生成，但明确禁止将原始高清图片作为图片素材包单独捆绑分发。正式公开发行包必须排除未经独立权利审查的原始素材文件，或改用用户本地受控资产目录（`WENGAO_ASSETS_ROOT`）。

---

## 三、自有代码所有权与发布阻塞项

1. **自有代码许可证**：**已决策（2026-11-04）——MIT**。项目所有者通过交互确认选定
   MIT；仓库根已写入 `LICENSE` 文件（版权行暂署「稳稿 PPT（WenGao PPT）项目所有者」，
   可由所有者改署法定名称）。现行口径见 `THIRD-PARTY-NOTICES.md` A 节；`LICENSE`
   已进安装载荷成员清单（`scripts/build_install_payload.py`）。
2. **GitHub 与 SkillHub 公开上架**：
   - 必须在获得用户明确授权且通过 Codex 独立验收复核后，方可启动发布。
   - 涉及第三方平台账户登录、双重认证（2FA）、实名认证、商业主体绑定及法律协议签署的操作，一律由用户亲自完成，代理程序不越权代办。
