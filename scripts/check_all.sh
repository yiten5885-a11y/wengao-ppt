#!/usr/bin/env bash
# scripts/check_all.sh —— Step 0.6：一条命令跑全（pytest + 契约 schema + 独占检查）。
#
# 用法：  bash scripts/check_all.sh [--base main] [--head HEAD]
# 退出码 0 = 全绿。任何一步失败都阻止合并。
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
BASE="main"; HEAD="HEAD"
while [ $# -gt 0 ]; do
  case "$1" in
    --base) BASE="$2"; shift 2;;
    --head) HEAD="$2"; shift 2;;
    *) echo "未知参数：$1" >&2; exit 2;;
  esac
done

echo "== 1/3 pytest =="
$PY -m pytest tests -q

echo "== 2/3 契约 schema 自检 =="
$PY - <<'EOF'
from wengao_ppt.contracts_rs import schema_ids, validate, ContractError
ids = schema_ids()
print(f"已登记契约：{len(ids)} 个 -> {', '.join(ids)}")
assert len(ids) >= 6, "rs 契约不足 6 个"
print("契约清单完整")
EOF

echo "== 3/3 文件独占检查 =="
$PY scripts/ownership_guard.py --base "$BASE" --head "$HEAD"

echo "== check_all 全部通过 =="
