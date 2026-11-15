#!/usr/bin/env bash
# 稳稿 PPT 技能·首次安装脚本。
# 幂等：已经装过就跳过对应步骤，可以放心重复执行。
# 不碰用户系统级软件（不装 LibreOffice、不改 PATH）；缺了就如实报告，
# 让 SKILL.md 里的指令把这件事转述给用户。
set -euo pipefail

INSTALL_ROOT="${WENGAO_SKILL_ROOT:-$HOME/.wengao-ppt-skill}"
REPO_URL="https://github.com/yiten5885-a11y/wengao-ppt.git"
REPO_DIR="$INSTALL_ROOT/wengao-ppt"

mkdir -p "$INSTALL_ROOT"

if [ -d "$REPO_DIR/.git" ]; then
  echo "[wengao-ppt-skill] 已存在，跳过 clone：$REPO_DIR"
else
  echo "[wengao-ppt-skill] clone：$REPO_URL -> $REPO_DIR"
  git clone "$REPO_URL" "$REPO_DIR"
fi

cd "$REPO_DIR"

echo "[wengao-ppt-skill] 同步 git submodule（vendor/luobo-ppt，上游 MIT 协议）"
git submodule update --init --recursive

if [ ! -x ".venv/bin/python" ]; then
  echo "[wengao-ppt-skill] 建虚拟环境"
  python3 -m venv .venv
fi

echo "[wengao-ppt-skill] 安装 Python 依赖"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "[wengao-ppt-skill] 安装完成，真实验证入口可用性："
PYTHONDONTWRITEBYTECODE=1 "$REPO_DIR/.venv/bin/python" -m wengao_ppt.cli --version

echo
echo "[wengao-ppt-skill] 检查 LibreOffice（渲染/导出硬依赖，本脚本不会代装）："
if command -v soffice >/dev/null 2>&1; then
  soffice --version
elif [ -x "/Applications/LibreOffice.app/Contents/MacOS/soffice" ]; then
  /Applications/LibreOffice.app/Contents/MacOS/soffice --version
elif [ -x "$HOME/.wengao_ppt/libreoffice/LibreOffice.app/Contents/MacOS/soffice" ]; then
  "$HOME/.wengao_ppt/libreoffice/LibreOffice.app/Contents/MacOS/soffice" --version
else
  echo "[wengao-ppt-skill] 未检测到 LibreOffice。请先从 https://www.libreoffice.org/download/download/ 安装，" \
       "否则 run/validate/export 都无法真实渲染。"
  exit 1
fi

echo
echo "[wengao-ppt-skill] 全部就绪。CLI 入口：$REPO_DIR/.venv/bin/python -m wengao_ppt.cli"
