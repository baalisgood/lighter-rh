#!/usr/bin/env bash
set -euo pipefail
umask 077
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
task_python="${STOCK_PYTHON:-${ARBITRAGE_PYTHON:-$project_dir/.venv/bin/python}}"
case "$task_python" in /*) ;; *) echo 'Python path must be absolute' >&2; exit 2 ;; esac
case "${1:-}" in check|monitor|live) ;; *) echo 'Usage: bash start.sh check|monitor|live' >&2; exit 2 ;; esac
[[ $# == 1 ]] || exit 2
# STOCK_VENUE: lighter_rh (默认，Lighter 主网 <-> Lighter Robinhood Chain，maker->taker) | arcus (旧版 Lighter <-> Arcus)
stock_venue="${STOCK_VENUE:-lighter_rh}"
case "$stock_venue" in lighter_rh|arcus) ;; *) echo 'STOCK_VENUE must be lighter_rh or arcus' >&2; exit 2 ;; esac
cd "$project_dir"
exec env -i PATH=/usr/bin:/bin LANG="${LANG:-en_US.UTF-8}" "$task_python" - "$1" "$stock_venue" <<'PY'
import fcntl, os, pathlib, sys, tempfile
import yaml
root = pathlib.Path.cwd()
mode = sys.argv[1]
venue = sys.argv[2]
names = {
    'lighter_rh': ('arbitrage_segmented_rh', 'monitor_lighter_rh', 'arbitrage_lighter_rh', 'multi_exchange_arbitrage_rh'),
    'arcus': ('arbitrage_segmented', 'monitor_lighter_arcus', 'arbitrage_lighter_arcus', 'multi_exchange_arbitrage'),
}[venue]
segmented_name, monitor_name, execution_name, multi_name = names
# The wrapper only isolates paths/environment and selects native monitor_only.
for name in ('.env', 'config', 'config/exchanges', 'config/exchanges/lighter_config.yaml',
             'config/exchanges/arcus_config.yaml', 'config/exchanges/lighter_rh_config.yaml', 'run', 'run/stock.lock', 'logs', 'data', 'core'):
    path = root / name
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise SystemExit('Refusing symlink/escaped local path: ' + name)
profiles = [segmented_name, monitor_name, execution_name, multi_name, 'multi_leg_pairs']
loaded = {}
for name in profiles:
    path = root / 'config/arbitrage' / (name + '.yaml')
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise SystemExit('Refusing escaped profile')
    value = yaml.safe_load(path.read_text())
    if not isinstance(value, dict):
        raise SystemExit('Invalid profile: ' + name)
    loaded[name] = value
sys.path.insert(0, str(root))
import importlib.util
spec = importlib.util.spec_from_file_location("stock_preflight", root / "core/services/arbitrage_monitor_v2/config/stock_preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)
validate_stock_profiles = preflight.validate_stock_profiles
validate_stock_profiles(root / 'config/arbitrage' / (segmented_name + '.yaml'),
                        root / 'config/arbitrage' / (execution_name + '.yaml'))
if mode == 'check':
    print('Venue:', venue)
    print('Local stock profiles and decision/execution parity OK; no account or network checks performed.')
    raise SystemExit(0)
(root / 'run').mkdir(exist_ok=True)
with (root / 'run/stock.lock').open('a') as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Stock process already running')
    config = loaded[segmented_name]
    config['system_mode']['monitor_only'] = mode != 'live'
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', dir=root / 'run', delete=False) as output:
        yaml.safe_dump(config, output, allow_unicode=True, sort_keys=False)
        runtime = output.name
    print('Native engine mode:', mode, 'venue:', venue, '(requires account authentication)', flush=True)
    # Keep the lock with the actual trading PID, including termination signals.
    os.set_inheritable(lock.fileno(), True)
    os.execv(sys.executable, [sys.executable, 'main_unified.py', '--config', runtime,
                            '--monitor-config', 'config/arbitrage/' + monitor_name + '.yaml',
                            '--execution-config', 'config/arbitrage/' + execution_name + '.yaml'])
PY
