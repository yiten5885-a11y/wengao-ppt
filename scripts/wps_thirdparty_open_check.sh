#!/bin/bash
# WPS Office 第三方兼容实测（2026-11-04，用户授权安装；PowerPoint 本体验证仍
# NOT_EVALUABLE——本机无 PowerPoint/WPS/Keynote，WPS 是本机条件下可获得的
# 第三方 MS 格式 Office 实现）。
#
# 对象：r10b 干净安装验收的最终导出件 cleanroom_first.wengao.pptx
# 判定：WPS 能打开并渲染（截图存证）+ 打开前后文件 SHA-256 不变（只读验证）。
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="$REPO/reports/install-cleanroom-20261104-r10b/first-task/export/cleanroom_first.wengao.pptx"
OUT="$REPO/reports/wps-compat-20261104"
DMG=/tmp/wps-arm64.dmg
mkdir -p "$OUT"

echo "== 1. 安装包校验 =="
file "$DMG"

echo "== 2. 挂载并安装到 /Applications =="
hdiutil attach "$DMG" -nobrowse -readonly >/dev/null
sleep 2
SRC_APP=$(ls -d /Volumes/*/*.app 2>/dev/null | head -1)
echo "dmg 内 app: $SRC_APP"
APP_NAME=$(basename "$SRC_APP")
rm -rf "/Applications/$APP_NAME"
cp -R "$SRC_APP" /Applications/
hdiutil detach "$(echo "$SRC_APP" | cut -d/ -f3)" -quiet 2>/dev/null
xattr -dr com.apple.quarantine "/Applications/$APP_NAME" 2>/dev/null
echo "== 3. 签名/公证核验 =="
spctl -a -vv "/Applications/$APP_NAME" 2>&1 | head -2

echo "== 4. 打开导出件（记录打开前 SHA）=="
BEFORE=$(shasum -a 256 "$TARGET" | awk '{print $1}')
echo "before_sha256=$BEFORE"
open -a "/Applications/$APP_NAME" "$TARGET"
echo "已发出 open，等待渲染…"
sleep 30
screencapture -x "$OUT/wps-open-1.png"
sleep 12
screencapture -x "$OUT/wps-open-2.png"

echo "== 5. 打开后 SHA（只读验证）=="
AFTER=$(shasum -a 256 "$TARGET" | awk '{print $1}')
echo "after_sha256=$AFTER"
if [ "$BEFORE" = "$AFTER" ]; then echo "SHA_UNCHANGED=yes"; else echo "SHA_UNCHANGED=no"; fi
echo "DONE（截图在 $OUT/，请人工/视觉核验渲染内容）"
