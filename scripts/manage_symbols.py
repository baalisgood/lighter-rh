#!/usr/bin/env python3
"""
快速增删 Lighter 主网 <-> Lighter RH 的套利交易对（一次性同步全部 RH 配置文件）。

用法（在项目根目录）：
  .venv/bin/python scripts/manage_symbols.py list
  .venv/bin/python scripts/manage_symbols.py add BTC ETH SOL HYPE            # 自动拉取两所公开市场参数
  .venv/bin/python scripts/manage_symbols.py add AAPL --stock --threshold 0.12 --step 0.07
  .venv/bin/python scripts/manage_symbols.py remove HYPE
  .venv/bin/python scripts/manage_symbols.py add BTC --dry-run               # 只预览，不写文件

同步的文件：arbitrage_segmented_rh.yaml / arbitrage_lighter_rh.yaml / monitor_lighter_rh.yaml /
multi_exchange_arbitrage_rh.yaml。写入前做本地预检（stock_preflight），失败则不落盘。
修改后需重启进程生效。删除交易对前请先确认两所该币种持仓已全部平掉。
"""
import argparse
import copy
import json
import sys
import tempfile
import urllib.request
from decimal import Decimal, ROUND_UP
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.services.arbitrage_monitor_v2.config.stock_preflight import validate_stock_profiles  # noqa: E402

PROFILE_DIR = ROOT / "config" / "arbitrage"
SEGMENTED = "arbitrage_segmented_rh.yaml"
EXECUTION = "arbitrage_lighter_rh.yaml"
MONITOR = "monitor_lighter_rh.yaml"
MULTI = "multi_exchange_arbitrage_rh.yaml"
VENUE_URLS = {
    "lighter": "https://mainnet.zklighter.elliot.ai",
    "lighter_rh": "https://api.rh.lighter.xyz",
}
CRYPTO_DEFAULTS = {"threshold": 0.05, "step": 0.03, "profit": 0.03}
STOCK_DEFAULTS = {"threshold": 0.12, "step": 0.07, "profit": 0.07}


def standard(symbol):
    base = symbol.strip().upper().split("-")[0]
    return base, f"{base}-USDC-PERP"


def fetch_markets(venue):
    url = VENUE_URLS[venue] + "/api/v1/orderBookDetails"
    with urllib.request.urlopen(url, timeout=20) as response:
        data = json.load(response)
    return {row["symbol"].upper(): row for row in data.get("order_book_details", [])
            if row.get("market_type", "perp") == "perp"}


def market_spec(base, markets, overrides):
    """Common quantity grid and minimums that satisfy both venues."""
    if overrides.precision is not None and overrides.min_qty is not None:
        return {"precision": overrides.precision, "min": {"lighter": Decimal(str(overrides.min_qty)),
                "lighter_rh": Decimal(str(overrides.min_qty))}, "notional": {"lighter": 10, "lighter_rh": 10},
                "price": Decimal(str(overrides.price or 0))}
    rows = {}
    for venue, table in markets.items():
        row = table.get(base)
        if row is None:
            raise SystemExit(f"{base} 在 {venue} 上没有永续市场，无法套利")
        if row.get("status") != "active":
            raise SystemExit(f"{base} 在 {venue} 上状态为 {row.get('status')}，无法套利")
        rows[venue] = row
    precision = min(int(row["supported_size_decimals"]) for row in rows.values())
    step = Decimal(1).scaleb(-precision)
    minimum = {venue: Decimal(str(row["min_base_amount"])) for venue, row in rows.items()}
    notional = {venue: max(1, int(Decimal(str(row.get("min_quote_amount") or 10)).to_integral_value(ROUND_UP)))
                for venue, row in rows.items()}
    price = Decimal(str(rows["lighter"].get("last_trade_price") or 0))
    # Each venue minimum is rounded up onto the common step.
    minimum = {venue: ((value / step).to_integral_value(ROUND_UP) * step).normalize() for venue, value in minimum.items()}
    return {"precision": precision, "min": minimum, "notional": notional, "price": price}


def load(name):
    return yaml.safe_load((PROFILE_DIR / name).read_text(encoding="utf-8"))


HEADER = "# Lighter 主网 (taker 对冲) <-> Lighter Robinhood Chain (maker 挂单)。金额/限额均为配置项。\n"


def dump(path, data):
    path.write_text(HEADER + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def as_number(value):
    value = Decimal(str(value))
    return int(value) if value == value.to_integral_value() else float(value.normalize())


def add_symbol(profiles, base, name, spec, args):
    segmented, execution, monitor, multi = profiles
    defaults = STOCK_DEFAULTS if args.stock else CRYPTO_DEFAULTS
    default_qty = segmented["default_config"]["quantity_config"]
    default_exec = execution["arbitrage_execution"]["quantity_config"]["default"]
    target = Decimal(str(args.target_value or default_qty["target_value_usdc"]))
    split = Decimal(str(args.order_value or default_qty["split_order_amount_usdc"]))
    max_position = Decimal(str(args.max_position or segmented["default_config"]["risk_config"]["max_position_value"]))
    step = Decimal(1).scaleb(-spec["precision"])
    common_min = max(spec["min"].values())
    if spec["price"] > 0:
        cheapest = max(Decimal(v) for v in spec["notional"].values()) / spec["price"]
        if split / spec["price"] < max(common_min, cheapest):
            raise SystemExit(f"{name}: 单笔金额 {split} USDC 低于两所最小下单量/最小名义金额")
    base_quantity = (target / spec["price"]).quantize(step) if spec["price"] > 0 else common_min
    grid = {
        "initial_spread_threshold": args.threshold if args.threshold is not None else defaults["threshold"],
        "grid_step": args.step if args.step is not None else defaults["step"],
        "max_segments": args.max_segments,
        "profit_per_segment": args.profit if args.profit is not None else defaults["profit"],
        "slippage_tolerance": 0.0001,
        "require_orderbook_liquidity": True,
        "min_orderbook_quantity": None,
    }
    if not args.stock:
        grid["stock_session"] = {"enabled": False}  # 24x7：不使用美股时段/盘后阈值
    segmented["symbol_configs"][name] = {
        "enabled": True,
        "grid_config": grid,
        "quantity_config": {
            "base_quantity": as_number(base_quantity),
            "quantity_mode": "value",
            "quantity_precision": spec["precision"],
            "target_value_usdc": as_number(target),
            "min_exchange_order_qty": {"default": as_number(common_min),
                                       **{venue: as_number(value) for venue, value in spec["min"].items()}},
            "split_order_amount_usdc": as_number(split),
        },
        "risk_config": {"max_position_value": float(max_position)},
    }
    segmented.setdefault("market_constraints", {})[base] = {
        "quantity_step": str(step),
        "min_quantity": str(common_min.normalize()),
        "min_notional_by_exchange": dict(spec["notional"]),
    }
    triggers = segmented["risk_control"]["open_only_close"]["price_volatility"].setdefault("trigger_symbols", [])
    if base not in triggers:
        triggers.append(base)
    entry = copy.deepcopy(default_exec)
    entry.update(quantity_precision=spec["precision"], quantity_mode="value", target_value_usdc=as_number(target),
                 single_order_amount_usdc=as_number(split), max_position_usdc=as_number(max_position))
    execution["arbitrage_execution"]["quantity_config"][name] = entry
    for profile in (monitor, multi):
        if name not in profile["symbols"]:
            profile["symbols"].append(name)


def remove_symbol(profiles, base, name):
    segmented, execution, monitor, multi = profiles
    if name not in segmented["symbol_configs"]:
        raise SystemExit(f"{name} 不在当前配置中")
    segmented["symbol_configs"].pop(name)
    segmented.get("market_constraints", {}).pop(base, None)
    triggers = segmented["risk_control"]["open_only_close"]["price_volatility"].get("trigger_symbols", [])
    if base in triggers:
        triggers.remove(base)
    execution["arbitrage_execution"]["quantity_config"].pop(name, None)
    for profile in (monitor, multi):
        if name in profile["symbols"]:
            profile["symbols"].remove(name)


def main(argv=None):
    parser = argparse.ArgumentParser(description="增删 Lighter <-> Lighter RH 套利交易对")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    add = sub.add_parser("add")
    add.add_argument("symbols", nargs="+")
    add.add_argument("--stock", action="store_true", help="美股代币：启用美股交易时段/盘后阈值（默认按 24x7 加密币处理）")
    add.add_argument("--threshold", type=float, help="开仓阈值 %%（加密默认 0.05，美股默认 0.12）")
    add.add_argument("--step", type=float, help="网格间距 %%")
    add.add_argument("--profit", type=float, help="每格止盈 %%")
    add.add_argument("--max-segments", type=int, default=3)
    add.add_argument("--target-value", type=float, help="每格目标金额 USDC（默认取 default_config）")
    add.add_argument("--order-value", type=float, help="单笔金额 USDC（默认取 default_config）")
    add.add_argument("--max-position", type=float, help="单币最大持仓 USDC")
    add.add_argument("--precision", type=int, help="离线模式：数量精度（需同时给 --min-qty）")
    add.add_argument("--min-qty", type=float, help="离线模式：最小下单量")
    add.add_argument("--price", type=float, help="离线模式：参考价格")
    add.add_argument("--replace", action="store_true", help="已存在时覆盖")
    add.add_argument("--dry-run", action="store_true")
    remove = sub.add_parser("remove")
    remove.add_argument("symbols", nargs="+")
    remove.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    names = (SEGMENTED, EXECUTION, MONITOR, MULTI)
    profiles = [load(name) for name in names]
    if args.command == "list":
        for name, item in profiles[0]["symbol_configs"].items():
            grid = item["grid_config"]
            kind = "24x7" if (grid.get("stock_session") or {}).get("enabled") is False else "stock"
            print(f"{name:18} {kind:5} T1={grid['initial_spread_threshold']}% step={grid['grid_step']}% "
                  f"order={item['quantity_config']['split_order_amount_usdc']}U enabled={item.get('enabled', True)}")
        return 0

    markets = None
    for raw in args.symbols:
        base, name = standard(raw)
        if args.command == "add":
            if name in profiles[0]["symbol_configs"] and not args.replace:
                raise SystemExit(f"{name} 已存在（用 --replace 覆盖）")
            if markets is None and not (args.precision is not None and args.min_qty is not None):
                markets = {venue: fetch_markets(venue) for venue in VENUE_URLS}
            spec = market_spec(base, markets or {}, args)
            add_symbol(profiles, base, name, spec, args)
            print(f"+ {name}: 精度={spec['precision']} 最小量={ {k: str(v) for k, v in spec['min'].items()} } "
                  f"最小名义={spec['notional']}")
        else:
            remove_symbol(profiles, base, name)
            print(f"- {name}（请确认两所该币种已无持仓）")
    if not profiles[0]["symbol_configs"]:
        raise SystemExit("至少需要保留一个交易对")

    # Validate on temporary copies first; only then write the real profiles.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_paths = [Path(tmp) / name for name in names]
        for path, data in zip(tmp_paths, profiles):
            dump(path, data)
        validate_stock_profiles(tmp_paths[0], tmp_paths[1])
    if args.dry_run:
        print("dry-run：预检通过，未写入文件")
        return 0
    for name, data in zip(names, profiles):
        dump(PROFILE_DIR / name, data)
    print("已更新配置并通过预检。重启进程后生效：bash start.sh monitor|live")
    return 0


if __name__ == "__main__":
    sys.exit(main())
