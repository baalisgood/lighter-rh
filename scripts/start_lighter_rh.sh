#!/usr/bin/env bash
# Lighter 主网 <-> Lighter Robinhood Chain（maker->taker），默认 monitor 模式。
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STOCK_VENUE=lighter_rh exec bash "$project_dir/start.sh" "${1:-monitor}"
