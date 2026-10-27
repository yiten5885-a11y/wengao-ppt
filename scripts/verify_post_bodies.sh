#!/usr/bin/env bash
# FIX-W4-001 现场验证：对运行中的 8823 发送带请求体的 POST，每条必须在 3 秒内返回。
# 用法：bash scripts/verify_post_bodies.sh [base_url]
# 只用于 8823 开发实例；不得指向 8821 正式入口。
set -u
BASE="${1:-http://127.0.0.1:8823}"
case "$BASE" in
  *:8821*) echo "拒绝：不得对 8821 正式入口运行。"; exit 2 ;;
esac
export NO_PROXY=127.0.0.1,localhost

fail=0
check() {
  local name="$1"; shift
  local out code
  out=$(curl -sS --noproxy '*' --max-time 3 -o /tmp/fixw4_body.$$ -w '%{http_code} %{time_total}' "$@" 2>&1)
  code=$?
  if [ $code -ne 0 ]; then
    echo "FAIL  ${name}  （超时或连接失败：${out}）"; fail=1; return
  fi
  echo "OK    ${name}  （HTTP/耗时：${out}）"
  head -c 300 /tmp/fixw4_body.$$; echo; echo
}

# 1) 材料上传：必须返回 uploaded: true
check "material/upload .md" -X POST -H 'X-Filename: fixw4-probe.md' \
  --data-binary $'# FIX-W4-001 探针\n\n营收同比增长 12%。\n' "$BASE/api/material/upload"
if ! grep -q '"uploaded": *true' /tmp/fixw4_body.$$ 2>/dev/null; then
  echo "FAIL  material/upload 未返回 uploaded: true"; fail=1
fi

# 2) PPTX 上传：内容不是合法 PPTX，期望快速返回 4xx（重点是不挂起）
check "upload 非法 pptx（期望快速 4xx）" -X POST -H 'X-Filename: fixw4-probe.pptx' \
  --data-binary 'PK-not-a-real-pptx' "$BASE/api/upload"

# 3) 开始制作：空参数，期望快速 4xx（重点是不挂起）
check "make 空参数（期望快速 4xx）" -X POST -H 'Content-Type: application/json' \
  --data-binary '{}' "$BASE/api/make"

# 4) 图片资产：非法扩展名，期望快速 400
check "assets 非法扩展名（期望快速 400）" -X POST -H 'X-Filename: fixw4-probe.txt' \
  --data-binary 'x' "$BASE/api/assets"

# 5) 清理接口：默认 dry-run，只列清单不动作
check "cleanup dry-run" -X POST -H 'Content-Type: application/json' \
  --data-binary '{}' "$BASE/api/cleanup"

rm -f /tmp/fixw4_body.$$
echo
if [ $fail -eq 0 ]; then
  echo "全部通过：带请求体的 POST 均在 3 秒内返回。"
  echo "注意：探针上传的 fixw4-probe.md 已进入数据根的上传目录，可用 /api/cleanup 回收。"
else
  echo "存在失败项。"; exit 1
fi
