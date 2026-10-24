#!/bin/sh
# 原生打开兼容路径 —— 监督 2026-10-20 原生缺字复核（native_v12_20261020_1544）的修复入口。
#
# 背景（诊断结论，详见 reports/source-completeness-20261020/原生缺字诊断.md）：
#   v12 原稿引用 Microsoft YaHei（本机缺失）+ Avenir Next + Hiragino Sans GB。
#   产品渲染路径用包内 fontconfig（assets/fontconfig/fonts.conf 把
#   Microsoft YaHei 强映射到 Hiragino Sans GB）得到真实中文字形；
#   直接打开 LibreOffice GUI 时没有这层环境，字体替换落到不含中文字形的字体上
#   → 普通编辑与放映画面出现缺字方框（产/钱/紧等），而 AX 文本完整。
#
# 本脚本给**原生 GUI 打开**注入同一层字体环境。边界（与渲染路径一致）：
#   - 只设本次进程的环境变量（FONTCONFIG_FILE / XDG_CACHE_HOME）；
#   - 不写 /etc、不改用户 fontconfig、不改全局 LibreOffice 设置；
#   - 不复制、不下载任何字体文件；被打开的文件字节不变（SHA 保持）。
#
# 用法：
#   scripts/open_native.sh <文件.pptx>        # 用本机 LibreOffice GUI 打开
#   WENGAO_DATA_ROOT=<数据根> scripts/open_native.sh <文件.pptx>
#   LO_BIN=<soffice 路径> WENGAO_PYTHON=<python> scripts/open_native.sh <文件.pptx>
#
# 监督 Cua 原生复核：用本脚本打开 exact 文件后照常 Start from First Slide /
# 关闭重开即可；本脚本不做任何点击/录屏，UI 操作归监督方。
set -eu

PROG="$(cd "$(dirname "$0")/.." && pwd)"
PY="${WENGAO_PYTHON:-python3}"
LO_BIN="${LO_BIN:-/Applications/LibreOffice.app/Contents/MacOS/soffice}"

if [ "$#" -lt 1 ]; then
    echo "用法: $0 <文件.pptx>" >&2
    exit 2
fi
TARGET="$1"
if [ ! -f "$TARGET" ]; then
    echo "文件不存在: $TARGET" >&2
    exit 2
fi

# 1) 取产品有效字体环境（与渲染子进程同一构造点：wengao_ppt.fontenv.build_env）
ENV_LINES="$("$PY" - "$PROG" <<'PYEOF'
import sys
sys.path.insert(0, sys.argv[1])
from wengao_ppt import fontenv
env, rec = fontenv.build_env()
if not rec.get("ok"):
    sys.stderr.write("[open_native] 字体环境不可用: %s\n" % rec.get("error"))
    sys.exit(3)
import shlex
print("FONTCONFIG_FILE=%s" % shlex.quote(str(env.get("FONTCONFIG_FILE", ""))))
print("XDG_CACHE_HOME=%s" % shlex.quote(str(env.get("XDG_CACHE_HOME", ""))))
sys.stderr.write("[open_native] 有效字体配置: %s\n" % rec.get("effective_conf"))
sys.stderr.write("[open_native] 平台字体目录: %s\n" % ", ".join(rec.get("platform_dirs") or []))
PYEOF
)"

# 2) 注入本次进程环境并启动 LibreOffice GUI（继承环境，不用 open，避免丢弃变量）
eval "$ENV_LINES"
export FONTCONFIG_FILE XDG_CACHE_HOME
echo "[open_native] 正在用原生 LibreOffice GUI 打开: $TARGET"
exec "$LO_BIN" "$TARGET"
