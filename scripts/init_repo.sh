#!/usr/bin/env bash
# scripts/init_repo.sh —— RS-0 Step 0.1：仓库与并发收口（在 Mac mini 本机执行一次）。
#
# 背景：任务书 §1 证据——项目目录此前不是 git 仓库；交付时曾发生另一进程并发改
# scene_images.py。本脚本建立唯一基线，并把"单一 owner 合并"规则落成可执行检查。
#
# 用法：  bash scripts/init_repo.sh
# 效果：
#   1. git init（若尚未初始化）
#   2. 写入 .gitignore（密钥/模型权重/输出/缓存）
#   3. 全量提交作为基线，并生成 SHA-256 清单 reports/rs-w4/baseline_manifest.sha256
#   4. 打印后续动作（worktree / tag 由 reports/handoff/STEP0_DONE.md 说明）
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -d .git ]; then
  git init -q
  git config user.email  "w4-owner@wengao.local" 2>/dev/null || true
  git config user.name   "W4 Owner" 2>/dev/null || true
  echo "[init_repo] git init 完成"
else
  echo "[init_repo] 已是 git 仓库，跳过 init"
fi

cat > .gitignore <<'EOF'
# 密钥（只走环境变量，绝不落盘）
.env
*.key
*.pem
*api_key*
# 模型权重与大文件输出
*.gguf
*.safetensors
*.ckpt
# 运行数据与缓存
__pycache__/
*.pyc
.venv/
*.log
*.pid
wb_loopback_data/
work/
workbench_uploads/
workbench_previews/
build/
.compare/
.DS_Store
EOF

git add -A
if git diff --cached --quiet; then
  echo "[init_repo] 没有新改动，基线已存在"
else
  git commit -q -m "baseline: wengao-visual-v13-material-fix 全量基线（RS-0 Step 0.1）"
  echo "[init_repo] 基线提交完成"
fi

mkdir -p reports/rs-w4
git ls-files -z | xargs -0 sha256sum > reports/rs-w4/baseline_manifest.sha256 2>/dev/null || true
echo "[init_repo] SHA-256 清单：reports/rs-w4/baseline_manifest.sha256（$(wc -l < reports/rs-w4/baseline_manifest.sha256 | tr -d ' ') 个文件）"
git log --oneline | head -3
echo "[init_repo] 下一步：按 reports/handoff/STEP0_DONE.md 建 worktree、打 tag rs-contracts-v1"
