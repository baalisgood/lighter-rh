#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STOCK_VENUE=arcus exec bash "$project_dir/start.sh" "${1:-monitor}"
