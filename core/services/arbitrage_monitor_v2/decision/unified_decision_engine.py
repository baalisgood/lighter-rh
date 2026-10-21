"""
分段套利统一决策引擎（重构版）

核心改进：
1. 总量驱动算法：target - actual = delta
2. 剥头皮状态机：触发、持有、止盈退出
3. 简化段管理：段仅用于记录，不参与决策
4. 多交易对独立：每个交易对独立配置和状态

算法流程：
1. 计算当前格子（基于价差）
2. 计算目标持仓（基于格子ID）
3. 计算操作差量（target - actual）
4. 执行拆单操作（开仓/平仓）
"""

from ..utils.stock_basis_cache import BasisCache, new_basis_state
import logging
import math
import time
from dataclasses import replace
from ..utils.stock_signal import cash_threshold
from .arbitrage_decision import _round_trip_fee_breakdown
from typing import Optional, Dict, Tuple, List
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING

from ..config.symbol_config import SegmentedConfigManager, SymbolConfig
from ..analysis.spread_calculator import SpreadData
from ..models import SegmentedPosition, PositionSegment, FundingRateData

from core.adapters.exchanges.utils.setup_logging import LoggingConfig

logger = LoggingConfig.setup_logger(
    name=__name__,
    log_file='unified_decision.log',
    console_formatter=None,  # 🔥 不输出到终端
    file_formatter='detailed',
    level=logging.INFO  # 💡 恢复日志打印，通过节流机制控制频率
)
# 🔥 额外确保不传播到父logger，防止终端抖动
logger.propagate = False


class UnifiedDecisionEngine:
    """统一决策引擎（总量驱动 + 剥头皮状态机）"""

    def __init__(self, config_manager: SegmentedConfigManager, *, deduct_fees: bool = True, exchange_fee_config=None, exchange_order_modes=None, basis_state_path=None):
        """
        初始化统一决策引擎

        Args:
            config_manager: 配置管理器（支持多交易对）
        """
        self.config_manager = config_manager
        self.exchange_order_modes = dict(exchange_order_modes or {})
        self.exchange_fee_config = dict(exchange_fee_config or {})
        self.deduct_fees = deduct_fees

        # 持仓管理（symbol级聚合）
        self._unfavorable_funding = {}
        self._basis_cache = BasisCache(basis_state_path, {
            symbol: config_manager.get_config(symbol).grid_config.basis_config
            for symbol in set(config_manager.symbol_configs) | set(getattr(config_manager, "_config_aliases", {}))
            if config_manager.get_config(symbol).grid_config.basis_config.get("enabled", False)
        }, logger)
        self._basis_states = self._basis_cache.load(time.time())
        self._signal_prices = {}
        self.positions: Dict[str, SegmentedPosition] = {}
        # 套利对级别持仓：symbol -> pair_key -> SegmentedPosition
        self.pair_positions: Dict[str, Dict[str, SegmentedPosition]] = {}
        # 记录各交易所对当前持仓的开仓方向（pair_key -> +1/-1）
        # pair_key 区分买/卖角色与交易所，避免同一 symbol 多交易所对串味
        self.open_direction: Dict[str, int] = {}

        # 剥头皮状态（每个交易对独立）
        self.scalping_active: Dict[str, bool] = {}

        # 价差持续性跟踪
        self._spread_persistence_state: Dict[str, Dict] = {}

        # 🔥 反向开仓检测标记（用于触发平仓检查）
        self._reverse_open_detected: bool = False

        # 精度控制
        self.quantity_epsilon = Decimal('0.00000001')
        self.price_epsilon = Decimal('0.00000001')

        # 拆单短缺缓存：记录未能成交的残余数量，等待下一次补齐
        self.pending_open_shortfall: Dict[str, Decimal] = {}

        # 上一次开仓信号的价格快照，避免同价位重复触发
        self._last_open_signal_prices: Dict[str,
                                            Tuple[Optional[Decimal], Optional[Decimal]]] = {}

        # 信号日志节流（开/平仓共用），默认30秒以减少刷屏但保持可见性
        self.signal_log_interval = 30.0

        # 日志节流：针对不同类型的日志使用不同的节流时间
        self._log_throttle_times: Dict[str, float] = {}

        # 错误避让控制器（外部注入）
        self._backoff_controller = None

        logger.info("✅ [统一决策] 统一决策引擎初始化完成")

        # 启动时打印一次网格阈值表，方便排查
        self._grid_thresholds_logged = False
        self._log_grid_thresholds_snapshot()

    # ========================================================================
    # 核心决策接口
    # ========================================================================

    def _config(self, symbol):
        config = self.config_manager.get_config(symbol)
        return replace(config, grid_config=replace(config.grid_config, initial_spread_threshold=cash_threshold(config.grid_config)))

    def observe_basis(self, symbol, orderbooks, now=None):
        cfg = self._config(symbol).grid_config.basis_config
        if not cfg.get("enabled", False):
            return
        state = self._basis_states.setdefault(symbol, new_basis_state(cfg))
        value = None
        try:
            mids = {}
            # Basis is always center (lighter mainnet) vs its counter venue (lighter_rh or legacy arcus).
            counters = [name for name in orderbooks if name != "lighter"]
            if len(counters) != 1:
                raise KeyError("stock basis requires lighter plus one counter venue")
            counter = counters[0]
            for exchange in ("lighter", counter):
                ob = orderbooks[exchange]
                stamp = getattr(ob, "received_timestamp", None) or getattr(ob, "timestamp", None)
                source_stamp = getattr(ob, "timestamp", None)
                clock = time.time() if now is None else now
                if stamp is None or source_stamp is None or any(not math.isfinite(ts.timestamp()) or not -1 <= clock - ts.timestamp() <= state.max_gap for ts in (stamp, source_stamp)):
                    raise ValueError("stale BBO")
                bid, ask = ob.bids[0], ob.asks[0]
                prices = [Decimal(str(bid.price)), Decimal(str(ask.price))]
                sizes = [Decimal(str(bid.size)), Decimal(str(ask.size))]
                if any(not x.is_finite() or x <= 0 for x in prices + sizes) or prices[0] > prices[1]:
                    raise ValueError("invalid BBO")
                mids[exchange] = sum(prices) / 2
            value = float((mids["lighter"] - mids[counter]) / mids[counter] * 100)
        except (KeyError, IndexError, ValueError, TypeError, AttributeError):
            pass
        clock = time.time() if now is None else now
        state.observe(clock, value)
        self._basis_cache.checkpoint(self._basis_states, clock)

    def basis_ready(self, symbol):
        if not self._config(symbol).grid_config.basis_config.get("enabled", False):
            return True
        state = self._basis_states.get(symbol)
        return bool(state and state.stable and state.last is not None and 0 <= time.time() - state.last <= state.max_gap)

    def adjusted_open_spread(self, symbol, spread):
        if not self._config(symbol).grid_config.basis_config.get("enabled", False):
            return spread.spread_pct
        state = self._basis_states.get(symbol)
        basis = state.ema if state and state.ready and state.ema is not None else 0.0
        return spread.spread_pct - basis * (1 if spread.exchange_sell.lower() == "lighter" else -1)

    def close_relative_spread(self, symbol, spread, direction=1):
        if self._config(symbol).grid_config.basis_config.get("enabled", False):
            # Closing buy is the original sell. Never infer direction from raw spread sign.
            state = self._basis_states.get(symbol)
            basis = state.ema if state and state.ready and state.ema is not None else 0.0
            return -spread.spread_pct - basis * (1 if spread.exchange_buy.lower() == "lighter" else -1)
        return -spread.spread_pct * (1 if direction >= 0 else -1)

    def funding_risk_reason(self, symbol, funding, now=None):
        config = self._config(symbol)
        now = time.time() if now is None else now
        if not config.grid_config.basis_config.get("funding_budget_enabled", False) or not config.risk_config.enable_funding_rate_risk or funding is None or funding.is_favorable_for_position or funding.funding_rate_diff < config.risk_config.funding_rate_diff_threshold:
            self._unfavorable_funding.pop(symbol, None)
            return None
        previous = self._unfavorable_funding.get(symbol)
        max_gap = float(config.grid_config.basis_config.get("funding_max_age_seconds", 300))
        start = previous[0] if previous and 0 <= now - previous[1] <= max_gap else now
        self._unfavorable_funding[symbol] = (start, now)
        if now - start >= config.risk_config.max_unfavorable_funding_hours * 3600:
            return "持续不利资金费率超过配置小时上限"
        return None

    def remaining_entry_notional(self, symbol):
        position = self.positions.get(symbol)
        if not position or position.total_quantity <= self.quantity_epsilon:
            return Decimal("0")
        result = Decimal("0")
        tracked = Decimal("0")
        for seg in position.get_open_segments():
            prices = [Decimal(str(seg.open_price_buy)), Decimal(str(seg.open_price_sell))]
            if any(not x.is_finite() or x <= 0 for x in prices):
                return None
            result += seg.open_quantity * max(prices)
            tracked += seg.open_quantity
        return result if abs(tracked - position.total_quantity) <= self.quantity_epsilon else None

    def _quantity_after_fifo_close(self, symbol, target_notional):
        position = self.positions.get(symbol)
        if not position:
            return Decimal("0")
        current = self.remaining_entry_notional(symbol)
        if current is None:
            return position.total_quantity if target_notional > 0 else Decimal("0")
        remove = max(Decimal("0"), current - target_notional)
        closed = Decimal("0")
        for seg in position.get_open_segments():
            price = max(seg.open_price_buy, seg.open_price_sell)
            take = min(seg.open_quantity, remove / price)
            closed += take
            remove -= take * price
            if remove <= self.quantity_epsilon:
                break
        return max(Decimal("0"), position.total_quantity - closed)

    def minimum_close_quantity(self, symbol, spread):
        cfg = self._config(symbol)
        minimum = max([cfg.quantity_config.min_order_size, *cfg.quantity_config.exchange_min_order_qty.values()])
        constraints = getattr(self.config_manager, "market_constraints", {}).get((spread.buy_symbol or spread.symbol).split("-")[0], {})
        low_price = min(spread.price_buy, spread.price_sell)
        if low_price > 0:
            minimum = max([minimum, *(Decimal(str(v)) / low_price for v in constraints.get("min_notional_by_exchange", {}).values())])
        step = Decimal(1).scaleb(-cfg.quantity_config.quantity_precision)
        return (minimum / step).to_integral_value(rounding=ROUND_CEILING) * step

    def block_close_remainder(self, symbol, quantity):
        if not hasattr(self, "close_blocked_reasons"):
            self.close_blocked_reasons = {}
        reason = "Manual review: exact reduce-only remainder below verified venue minima"
        self.close_blocked_reasons[symbol] = reason
        now = time.time()
        key = f"dust:{symbol}"
        if now - self._log_throttle_times.get(key, 0) >= 300:
            logger.warning("⛔️ [%s] %s; quantity=%s", symbol, reason, quantity)
            self._log_throttle_times[key] = now

    def expected_funding_cost(self, symbol, funding):
        cfg = self._config(symbol)
        basis = cfg.grid_config.basis_config
        if not basis.get("funding_budget_enabled", False) or not cfg.risk_config.enable_funding_rate_risk:
            return 0.0
        if funding is None:
            return None
        horizon = float(basis.get("funding_horizon_hours", 1))
        buy, sell = float(funding.funding_rate_buy), float(funding.funding_rate_sell)
        if not all(math.isfinite(value) for value in (horizon, buy, sell)) or horizon <= 0:
            return None
        return max(0.0, buy - sell) * horizon

    def preview_order_quantity(self, symbol, spread, funding, *, is_open):
        cfg = self._config(symbol)
        price = max(spread.price_buy, spread.price_sell)
        if not price.is_finite() or price <= 0:
            return Decimal("0")
        if is_open:
            value = self.adjusted_open_spread(symbol, spread) - self._round_trip_fee_pct(spread)
            funding_cost = self.expected_funding_cost(symbol, funding)
            if funding_cost is None:
                return Decimal("0")
            value -= funding_cost
            grid = min(self._calculate_current_grid(symbol, value), cfg.grid_config.max_segments)
            occupied = self.remaining_entry_notional(symbol)
            if grid <= 0 or occupied is None:
                return Decimal("0")
            budget = min(cfg.quantity_config.target_value_usdc * grid, cfg.risk_config.max_position_value)
            available = max(Decimal("0"), budget - occupied)
            price *= (1 + Decimal(str(cfg.grid_config.slippage_tolerance or 0))) ** 2
            return self._format_quantity(min(available, cfg.quantity_config.split_order_amount_usdc) / price, cfg.quantity_config.quantity_precision)
        self._signal_prices[symbol] = price
        if not hasattr(self, "_close_minimums"):
            self._close_minimums = {}
        self._close_minimums[symbol] = self.minimum_close_quantity(symbol, spread)
        actual = self._get_actual_position(symbol)
        state = self._basis_states.get(symbol)
        if cfg.grid_config.basis_config.get("enabled", False) and not (state and state.ready and state.last is not None and 0 <= time.time() - state.last <= state.max_gap):
            delta = actual
        else:
            target = self._calculate_target_position_by_spread(symbol, self.close_relative_spread(symbol, spread), cfg)
            delta = max(Decimal("0"), actual - target)
        return self._calculate_order_quantity(symbol, delta, cfg, is_close=True)

    async def should_open(
        self,
        symbol: str,
        spread_data: SpreadData,
        funding_rate_data: Optional[FundingRateData] = None,
        *,
        skip_persistence: bool = False,
    ) -> Tuple[bool, Decimal]:
        """
        判断是否应该开仓

        Returns:
            (是否开仓, 开仓数量)
        """
        config = self._config(symbol)
        position = self.positions.get(symbol)
        if config.quantity_config.quantity_mode == "value" and position and 0 < position.total_quantity < self.minimum_close_quantity(symbol, spread_data):
            self.block_close_remainder(symbol, position.total_quantity)
            return False, Decimal("0")
        if symbol in getattr(self, "close_blocked_reasons", {}):
            return False, Decimal("0")
        if not self.basis_ready(symbol):
            self._reset_spread_persistence(self._build_persistence_key(symbol, spread_data))
            return False, Decimal("0")
        basis_cfg = config.grid_config.basis_config
        funding_cost = self.expected_funding_cost(symbol, funding_rate_data)
        if funding_cost is None:
            self._log_info_throttle(f"funding_unavailable_{symbol}", f"{symbol} 资金费率不可用，等待有效且不超过300秒的费率", interval=60)
            self._reset_spread_persistence(self._build_persistence_key(symbol, spread_data))
            return False, Decimal("0")
        self._signal_prices[symbol] = max(Decimal(str(spread_data.price_buy)), Decimal(str(spread_data.price_sell))) * (1 + Decimal(str(config.grid_config.slippage_tolerance or 0))) ** 2
        spread_data = replace(spread_data, spread_pct=self.adjusted_open_spread(symbol, spread_data) - self._round_trip_fee_pct(spread_data) - funding_cost)

        # 🔥 检查交易所是否处于错误避让状态（日志节流在 backoff_controller 中处理）
        if self._backoff_controller:
            exchanges = [spread_data.exchange_buy, spread_data.exchange_sell]
            for exchange in exchanges:
                if exchange and self._backoff_controller.is_paused(exchange):
                    return False, Decimal('0')
        else:
            # 🔥 调试：backoff_controller 未初始化
            logger.warning(
                f"⚠️ [DEBUG] {symbol} 开仓检查: backoff_controller={self._backoff_controller}")

        config = self._config(symbol)

        # 1. 计算当前格子
        current_grid = self._calculate_current_grid(
            symbol, spread_data.spread_pct)
        persistence_key = self._build_persistence_key(symbol, spread_data)
        pair_key = self._build_position_key(
            symbol,
            spread_data.exchange_buy,
            spread_data.exchange_sell,
            spread_data.buy_symbol or symbol,
            spread_data.sell_symbol or symbol,
        )
        current_buy_price = (
            Decimal(str(spread_data.price_buy))
            if spread_data.price_buy is not None
            else None
        )
        current_sell_price = (
            Decimal(str(spread_data.price_sell))
            if spread_data.price_sell is not None
            else None
        )

        # 构建价格快照（用于监测日志）
        price_snapshot = (
            f"开仓视角: 买{spread_data.exchange_buy}/{spread_data.buy_symbol or symbol}@{spread_data.price_buy:.2f} → "
            f"卖{spread_data.exchange_sell}/{spread_data.sell_symbol or symbol}@{spread_data.price_sell:.2f}"
        )

        if current_grid == 0:
            # 价差不足，打印监测日志
            # 🔥 使用交易所组合作为key,避免1对多模式下日志被节流
            log_key = f"{symbol}_{spread_data.exchange_buy}_{spread_data.exchange_sell}_open_status"
            # 监测类日志默认降级为 DEBUG，减少 unified_decision.log 噪音
            self._log_debug_throttle(
                log_key,
                (
                    f"🔍 [{symbol}] 开仓监测\n"
                    f"   当前价差: {spread_data.spread_pct:+.4f}%\n"
                    f"   开仓阈值: ≥{config.grid_config.initial_spread_threshold:.4f}% (T1)\n"
                    f"   状态: ⏳ 等待价差扩大\n"
                    f"   {price_snapshot}"
                ),
                interval=120  # 2分钟打印一次
            )
            self._reset_spread_persistence(persistence_key)
            return False, Decimal('0')

        # 2. 计算开仓阈值
        threshold = self._calculate_open_threshold(
            symbol, current_grid, config)

        # 3. 检查价差是否达到阈值
        if spread_data.spread_pct < threshold:
            # 价差不足，打印监测日志
            # 🔥 使用交易所组合作为key,避免1对多模式下日志被节流
            log_key = f"{symbol}_{spread_data.exchange_buy}_{spread_data.exchange_sell}_open_status"
            # 监测类日志默认降级为 DEBUG，减少 unified_decision.log 噪音
            self._log_debug_throttle(
                log_key,
                (
                    f"🔍 [{symbol}] 开仓监测\n"
                    f"   当前价差: {spread_data.spread_pct:+.4f}%\n"
                    f"   开仓阈值: ≥{threshold:.4f}% (T{current_grid})\n"
                    f"   状态: ⏳ 等待价差扩大\n"
                    f"   {price_snapshot}"
                ),
                interval=120  # 2分钟打印一次
            )
            self._reset_spread_persistence(persistence_key)
            return False, Decimal('0')

        # 4. 检查价差持续性
        if not skip_persistence:
            if not self._check_spread_persistence(persistence_key, spread_data.spread_pct, threshold, config):
                # 持续性未满足，打印监测日志
                status_text = "✅ 满足条件(计时中)" if spread_data.spread_pct >= threshold else "⏳ 等待价差扩大"
                # 🔥 使用交易所组合作为key,避免1对多模式下日志被节流
                log_key = f"{symbol}_{spread_data.exchange_buy}_{spread_data.exchange_sell}_open_status"
                # 监测类日志默认降级为 DEBUG，减少 unified_decision.log 噪音
                self._log_debug_throttle(
                    log_key,
                    (
                        f"🔍 [{symbol}] 开仓监测\n"
                        f"   当前价差: {spread_data.spread_pct:+.4f}%\n"
                        f"   开仓阈值: ≥{threshold:.4f}% (T{current_grid})\n"
                        f"   状态: {status_text}\n"
                        f"   {price_snapshot}"
                    ),
                    interval=120  # 2分钟打印一次
                )
                return False, Decimal('0')

        # 3. 检查剥头皮激活
        self._check_scalping_activation(symbol, current_grid, config)

        # 4. 计算目标持仓
        target = self._calculate_target_position(symbol, current_grid, config)

        # 5. 计算当前持仓（真实持仓 + 待补短缺）
        actual = self._get_actual_position(symbol)
        carry = self.pending_open_shortfall.get(symbol, Decimal('0'))
        effective_actual = actual + carry

        pair_key = self._build_position_key(
            symbol,
            spread_data.exchange_buy,
            spread_data.exchange_sell,
            spread_data.buy_symbol or symbol,
            spread_data.sell_symbol or symbol,
        )
        direction = self.open_direction.get(pair_key)
        if (
            direction is not None
            and actual > self.quantity_epsilon
            and Decimal(str(spread_data.spread_pct)) * Decimal(direction) < Decimal("0")
        ):
            log_key = f"{symbol}_{spread_data.exchange_buy}_{spread_data.exchange_sell}_open_status"
            self._log_info_throttle(
                log_key,
                (
                    f"⏸️ [{symbol}] 开仓方向与当前价差相反，"
                    f"优先等待平仓：当前价差={spread_data.spread_pct:+.4f}%，"
                    f"记录方向={'正' if direction > 0 else '负'}"
                ),
                interval=30,
            )
            self._reset_spread_persistence(persistence_key)
            return False, Decimal("0")

        # 5.2 检查是否存在同一交易所对的反向持仓
        # 🔥 允许：lighter+paradex 和 edgex+lighter 同时存在（不同交易所对的1对多套利）
        # 🔥 禁止：lighter+paradex 和 paradex+lighter 同时存在（同一交易所对的双向持仓）
        if actual > self.quantity_epsilon:
            pair_map = self._get_pair_position_map(symbol)
            current_pair_key = self._build_position_key(
                symbol,
                spread_data.exchange_buy,
                spread_data.exchange_sell,
                spread_data.buy_symbol or symbol,
                spread_data.sell_symbol or symbol,
            )
            exchange_a = (spread_data.exchange_buy or "").lower()
            exchange_b = (spread_data.exchange_sell or "").lower()

            for existing_pair_key, existing_position in pair_map.items():
                if existing_position.total_quantity <= self.quantity_epsilon:
                    continue

                existing_buy = (existing_position.exchange_buy or "").lower()
                existing_sell = (existing_position.exchange_sell or "").lower()

                # 检查是否是同一对交易所（不论顺序）
                exchanges_current = {exchange_a, exchange_b}
                exchanges_existing = {existing_buy, existing_sell}

                if exchanges_current == exchanges_existing:
                    # 同一对交易所（包含 lighter/lighter），pair_key 不同即视为反向/混向，拒绝开仓
                    is_same_direction = (
                        exchange_a == existing_buy and exchange_b == existing_sell)
                    is_same_pair_key = (current_pair_key == existing_pair_key)

                    if (not is_same_direction) or (not is_same_pair_key):
                        # 🔥 显示完整的币种信息
                        existing_buy_sym = existing_position.buy_symbol or symbol
                        existing_sell_sym = existing_position.sell_symbol or symbol
                        current_buy_sym = spread_data.buy_symbol or symbol
                        current_sell_sym = spread_data.sell_symbol or symbol

                        log_key = f"{symbol}_{exchange_a}_{exchange_b}_reverse_pair"
                        self._log_info_throttle(
                            log_key,
                            (
                                f"⏸️ [{symbol}] 检测到反向开仓信号（实为平仓信号）：\n"
                                f"   现有持仓: 买{existing_buy}/{existing_buy_sym}→卖{existing_sell}/{existing_sell_sym} (数量={existing_position.total_quantity})\n"
                                f"   当前信号: 买{exchange_a}/{current_buy_sym}→卖{exchange_b}/{current_sell_sym}\n"
                                f"   → 触发平仓检查（价差反转后小于阈值将执行平仓）"
                            ),
                            interval=30,
                        )
                        # 🔥 设置标记，告知上层应该立即检查平仓
                        self._reverse_open_detected = True
                        self._reset_spread_persistence(persistence_key)
                        return False, Decimal("0")

        # 6. 计算差量（仅针对新增目标，不包含短缺部分）
        delta = target - effective_actual
        if config.quantity_config.quantity_mode == "value":
            occupied = self.remaining_entry_notional(symbol)
            price = self._signal_prices.get(symbol, Decimal("0"))
            if occupied is None or not price.is_finite() or price <= 0:
                return False, Decimal("0")
            budget = min(config.quantity_config.target_value_usdc * min(current_grid, config.grid_config.max_segments), config.risk_config.max_position_value)
            remaining_budget = max(Decimal("0"), budget - occupied)
            rounding_tolerance = price * Decimal(1).scaleb(-config.quantity_config.quantity_precision)
            protection_reserve = budget * 2 * Decimal(str(config.grid_config.slippage_tolerance or 0))
            if remaining_budget <= rounding_tolerance + protection_reserve:
                return False, Decimal("0")
            delta = min(remaining_budget, config.quantity_config.split_order_amount_usdc) / price
            carry = Decimal("0")

        if delta <= self.quantity_epsilon:
            return False, Decimal('0')  # 无需开仓（只剩短缺，等待下一格触发）

        # 7. 检查是否超过最大格子（超出仅补齐最大格子的目标持仓，不再扩张）
        max_segments = config.grid_config.max_segments
        if current_grid > max_segments:
            log_key = f"{symbol}_grid_cap"
            self._log_info_throttle(
                log_key,
                (
                    f"[{symbol}] 当前格子{current_grid}超过最大{max_segments}，"
                    f"仅补齐最大格子目标持仓={target}，当前有效持仓={effective_actual}"
                ),
                interval=60
            )

        # 8. 计算本次开仓数量（拆单 + 补齐短缺）
        order_qty = self._calculate_order_quantity(
            symbol, delta, config, carry)

        if order_qty <= self.quantity_epsilon:
            return False, Decimal('0')

        # 🔥 开仓信号日志节流：按symbol节流（不按格子），避免格子频繁切换导致刷屏
        log_key = f"{symbol}_open_signal"
        self._log_info_throttle(
            log_key,
            f"✅ [{symbol}] 开仓信号: 格子T{current_grid} | 目标={target} 实际={actual} 待补={carry} | 新增={delta} 本次={order_qty}",
            interval=60.0  # 🔥 增加到60秒，减少刷屏
        )

        self._last_open_signal_prices[pair_key] = (
            current_buy_price,
            current_sell_price,
        )

        return True, order_qty

    def is_last_split_order(
        self,
        symbol: str,
        order_quantity: Decimal,
        is_open: bool
    ) -> bool:
        """
        判断当前订单是否为最后一笔拆单

        Args:
            symbol: 交易对
            order_quantity: 本次订单数量
            is_open: 是否开仓

        Returns:
            是否为最后一笔拆单
        """
        # 如果是单笔订单（不拆单），直接返回True
        # 判断方法：本次数量 >= 剩余差量

        # 获取当前持仓
        actual = self._get_actual_position(symbol)

        if is_open:
            # 开仓：判断是否还有剩余差量
            # 简化判断：如果本次订单后，不再需要开仓，则是最后一笔
            # 这里我们通过检查拆单配置来判断
            config = self._config(symbol)
            min_order_qty = Decimal(
                str(config.grid_config.min_partial_order_quantity))

            # 如果本次数量小于最小拆单单位的2倍，很可能是最后一笔
            # （因为剩余差量不足以再拆一单）
            return order_quantity < min_order_qty * Decimal('2')
        else:
            # 平仓：判断平仓后是否还有持仓
            # 如果平仓后持仓接近0，则是最后一笔
            remaining = actual - order_quantity
            return remaining < self.quantity_epsilon

        return False

    async def should_close(
        self,
        symbol: str,
        spread_data: SpreadData,
        funding_rate_data: Optional[FundingRateData] = None,
        *,
        skip_persistence: bool = False,
    ) -> Tuple[bool, Decimal, str, Optional[int]]:
        """
        判断是否应该平仓

        Returns:
            (是否平仓, 平仓数量, 平仓原因, 段ID)
        """
        # 🔥 检查交易所是否处于错误避让状态（日志节流在 backoff_controller 中处理）
        if self._backoff_controller:
            exchanges = [spread_data.exchange_buy, spread_data.exchange_sell]
            for exchange in exchanges:
                if exchange and self._backoff_controller.is_paused(exchange):
                    return False, Decimal('0'), "", None

        config = self._config(symbol)
        self._signal_prices[symbol] = max(Decimal(str(spread_data.price_buy)), Decimal(str(spread_data.price_sell)))
        if not hasattr(self, "_close_minimums"):
            self._close_minimums = {}
        self._close_minimums[symbol] = self.minimum_close_quantity(symbol, spread_data)

        # 1. 检查是否有持仓
        actual = self._get_actual_position(symbol)
        if actual <= self.quantity_epsilon:
            return False, Decimal('0'), "", None

        if config.grid_config.basis_config.get("enabled", False):
            state = self._basis_states.get(symbol)
            usable = bool(state and state.ready and state.last is not None and 0 <= time.time() - state.last <= state.max_gap)
            if not usable:
                qty = self._calculate_order_quantity(symbol, actual, config, is_close=True)
                position = self.positions[symbol]
                left, profit = qty, Decimal("0")
                for segment in position.get_open_segments():
                    take = min(left, segment.open_quantity)
                    if min(segment.open_price_buy, segment.open_price_sell) <= 0:
                        return False, Decimal("0"), "", None
                    profit += take * (spread_data.price_sell - segment.open_price_buy + segment.open_price_sell - spread_data.price_buy)
                    entry_fees = (segment.entry_fee_buy_per_unit or Decimal("0")) + (segment.entry_fee_sell_per_unit or Decimal("0"))
                    profit -= take * entry_fees
                    left -= take
                    if left <= self.quantity_epsilon:
                        break
                conservative = qty * max(spread_data.price_buy, spread_data.price_sell) * (Decimal(str(self._round_trip_fee_pct(spread_data))) / 100 + 2 * Decimal(str(config.grid_config.slippage_tolerance or 0)))
                if left <= self.quantity_epsilon and profit > conservative:
                    return True, qty, "FIFO profit fallback (basis unavailable)", None
                return False, Decimal("0"), "", None

        # 2. 计算当前格子
        current_grid = self._calculate_current_grid(
            symbol, spread_data.spread_pct)

        # 3. 判断平仓逻辑
        is_scalping = self.scalping_active.get(symbol, False)

        if is_scalping:
            # 剥头皮模式：检查盈利止盈
            return await self._check_scalping_close(
                symbol, current_grid, spread_data, config
            )
        else:
            # 网格模式：跟随价差平仓
            return await self._check_grid_close(
                symbol,
                current_grid,
                spread_data,
                config,
                skip_persistence=skip_persistence,
            )

    # ========================================================================
    # 核心算法：总量驱动
    # ========================================================================

    def _calculate_current_grid(self, symbol: str, spread_pct: float) -> int:
        """
        计算当前所在格子

        Returns:
            格子ID（0表示价差不足）
        """
        config = self._config(symbol)

        if spread_pct < config.grid_config.initial_spread_threshold:
            return 0

        diff = spread_pct - config.grid_config.initial_spread_threshold
        grid = int(diff / config.grid_config.grid_step) + 1

        return grid

    def _calculate_open_threshold(self, symbol: str, grid: int, config: SymbolConfig) -> float:
        """
        计算开仓阈值

        Args:
            symbol: 交易对
            grid: 格子ID
            config: 配置

        Returns:
            开仓阈值（%）
        """
        if grid <= 0:
            return config.grid_config.initial_spread_threshold

        # 计算该格子的开仓阈值
        threshold = config.grid_config.initial_spread_threshold + \
            (grid - 1) * config.grid_config.grid_step
        return threshold

    def _calculate_target_position(
        self,
        symbol: str,
        grid: int,
        config: SymbolConfig
    ) -> Decimal:
        """
        计算目标持仓数量（基于格子ID）

        核心公式：
        - 固定数量模式：grid * base_quantity
        - 按金额模式：grid * (target_value / current_price)
        """
        if grid <= 0:
            return Decimal('0')

        # 限制在最大格子内
        effective_grid = min(grid, config.grid_config.max_segments)

        if config.quantity_config.quantity_mode == "fixed":
            # 固定数量模式
            return Decimal(str(effective_grid)) * config.quantity_config.base_quantity

        elif config.quantity_config.quantity_mode == "value":
            # 按金额模式（需要当前价格）
            current_price = self._signal_prices.get(symbol, Decimal("0"))
            if current_price <= Decimal('0'):
                return self._get_actual_position(symbol)

            target_value_per_grid = config.quantity_config.target_value_usdc
            quantity_per_grid = target_value_per_grid / current_price

            return Decimal(str(effective_grid)) * quantity_per_grid

        return Decimal('0')

    def _get_actual_position(self, symbol: str) -> Decimal:
        """获取当前实际持仓数量"""
        position = self.positions.get(symbol)
        if not position:
            return Decimal('0')

        return position.total_quantity

    def _calculate_order_quantity(
        self,
        symbol: str,
        delta: Decimal,
        config: SymbolConfig,
        carry: Decimal = Decimal('0'),
        *, is_close: bool = False,
    ) -> Decimal:
        """
        计算本次订单数量（拆单）

        Args:
            delta: 目标与实际的差量
            config: 配置

        Returns:
            本次订单数量

        拆单逻辑（优化后）:
        1. 优先使用 split_order_size（新参数）：
           - 如果设置了 split_order_size，直接使用该值作为单笔数量
           - ✅ 无论 split_order_size 是否大于 base_quantity，都按 split_order_size 做“单笔上限”
        2. 向后兼容旧参数：
           - 如果没有 split_order_size，使用旧逻辑（ratio + min_partial_order_quantity）
           - partial_ratio >= 1.0: 不拆单
           - partial_ratio < 1.0: 拆单，使用 min_partial_order_quantity

        示例（新参数）:
        - base_quantity=0.006, split_order_size=0.003
        - 第1笔: 0.003, 第2笔: 0.003
        """
        abs_delta = abs(delta)
        if abs_delta <= self.quantity_epsilon:
            return Decimal("0")
        raw_needed = abs_delta + carry

        if config.quantity_config.quantity_mode == "value":
            price = self._signal_prices.get(symbol, Decimal("0"))
            if price <= 0:
                position = self.positions.get(symbol)
                price = max((max(seg.open_price_buy, seg.open_price_sell) for seg in position.get_open_segments()), default=Decimal("0")) if position else Decimal("0")
            base_order = min(abs_delta, config.quantity_config.split_order_amount_usdc / price) if price > 0 else abs_delta
            carry = Decimal("0")
        else:
            base_order = self._calculate_split_quantity_core(abs_delta, config)
        order_qty = base_order + carry
        order_qty = min(order_qty, raw_needed)

        # 精度控制
        precision = config.quantity_config.quantity_precision
        order_qty = self._format_quantity(order_qty, precision)

        min_order = config.quantity_config.min_order_size
        if config.quantity_config.quantity_mode == "value":
            min_order = max([min_order, *config.quantity_config.exchange_min_order_qty.values()])
            if is_close:
                min_order = max(min_order, getattr(self, "_close_minimums", {}).get(symbol, Decimal("0")))
                total = self._get_actual_position(symbol)
                tail = total - order_qty
                if 0 < tail < min_order and order_qty < total:
                    order_qty = self._format_quantity(max(Decimal("0"), total - min_order), precision)
                if 0 < total < min_order:
                    self.block_close_remainder(symbol, total)
                    return Decimal("0")
                getattr(self, "close_blocked_reasons", {}).pop(symbol, None)
                if order_qty < min_order:
                    # A tiny grid trim is not an untradeable entire position.
                    return Decimal("0")
        if min_order and order_qty < min_order - self.quantity_epsilon:
            # 保存短缺，等待下一次与新拆单合并
            self.pending_open_shortfall[symbol] = raw_needed
            logger.info(
                f"⏸️ [{symbol}] 本次所需 {raw_needed} 低于最小下单量 {min_order}，"
                "累积到下一次开仓"
            )
            return Decimal('0')

        # 已准备随下一笔一起补齐的短缺被消化
        self.pending_open_shortfall[symbol] = Decimal('0')
        return order_qty

    def _calculate_split_quantity_core(
        self,
        available: Decimal,
        config: SymbolConfig
    ) -> Decimal:
        """根据拆单配置计算基础下单量"""
        if available <= self.quantity_epsilon:
            return Decimal('0')

        split_size = config.grid_config.split_order_size
        if split_size is not None and split_size > 0:
            split_qty = Decimal(str(split_size))
            # ✅ 执行层单笔上限：无论 split_order_size 是否大于 base_quantity，都按 split_order_size 限制单次下单量
            # 说明：base_quantity 代表“目标仓位/每格期望数量”，split_order_size 代表“实际执行的单笔数量上限”。
            order_qty = min(split_qty, available)
        else:
            partial_ratio = Decimal(
                str(config.grid_config.segment_partial_order_ratio))
            min_qty = Decimal(
                str(config.grid_config.min_partial_order_quantity))
            if partial_ratio >= Decimal('1.0'):
                order_qty = available
            else:
                if min_qty > Decimal('0'):
                    order_qty = min(min_qty, available)
                else:
                    order_qty = available * partial_ratio
                    order_qty = min(order_qty, available)
        return order_qty

    # ========================================================================
    # 剥头皮模式逻辑
    # ========================================================================

    def _check_scalping_activation(
        self,
        symbol: str,
        current_grid: int,
        config: SymbolConfig
    ):
        """检查是否激活剥头皮模式"""
        if not config.grid_config.scalping_enabled:
            return

        if self.scalping_active.get(symbol, False):
            return  # 已激活

        if current_grid >= config.grid_config.scalping_trigger_segment:
            self.scalping_active[symbol] = True
            logger.warning(
                f"🔴 [{symbol}] 剥头皮模式激活！"
                f"当前格子{current_grid} >= 触发格子{config.grid_config.scalping_trigger_segment}"
            )

    async def _check_scalping_close(
        self,
        symbol: str,
        current_grid: int,
        spread_data: SpreadData,
        config: SymbolConfig
    ) -> Tuple[bool, Decimal, str, Optional[int]]:
        """
        剥头皮模式平仓逻辑

        规则：
        1. 计算当前盈利
        2. 如果盈利达标，平仓到当前格子持仓
        3. 平仓后退出剥头皮模式
        """
        # 计算目标持仓（当前格子）
        target = self._calculate_target_position(symbol, current_grid, config)
        actual = self._get_actual_position(symbol)

        delta = target - actual

        if delta >= Decimal('0'):
            # 不需要平仓（价差扩大或持平）
            return False, Decimal('0'), "", None

        # 计算盈利
        profit_pct = self._calculate_profit(symbol, spread_data.spread_pct)

        if profit_pct < config.grid_config.scalping_profit_threshold:
            # 盈利未达标，继续持有
            return False, Decimal('0'), "", None

        # 盈利达标，触发平仓
        close_amount = abs(delta)
        close_qty = self._calculate_order_quantity(symbol, delta, config, is_close=True)

        # 🔥 标记退出剥头皮（在记录平仓时会实际退出）
        # 🔥 添加网格级别信息，与网格平仓保持一致
        reason = f"剥头皮止盈T{current_grid}(盈利{profit_pct:.3f}% >= 阈值{config.grid_config.scalping_profit_threshold}%)"

        logger.info(
            f"🛑 [{symbol}] {reason}, "
            f"平仓{close_amount}到目标{target}"
        )

        return True, close_qty, reason, None

    async def _check_grid_close(
        self,
        symbol: str,
        current_grid: int,
        spread_data: SpreadData,
        config: SymbolConfig,
        *,
        skip_persistence: bool = False,
    ) -> Tuple[bool, Decimal, str, Optional[int]]:
        """
        🔥 V2简化平仓逻辑：总量驱动

        核心思路：
        1. 根据当前价差，计算目标持仓
        2. 对比实际持仓，计算需要平仓的数量
        3. 不需要关心"段"，只看总量

        规则：
        - 价差 >= T3 (0.11%) → 目标持仓 0.0012 (T1+T2+T3)
        - 价差 >= T2 (0.08%) → 目标持仓 0.0008 (T1+T2)
        - 价差 >= T1 (0.05%) → 目标持仓 0.0004 (T1)
        - 价差 >= T0 (0.005%) → 目标持仓 0.0004 (保持T1)
        - 价差 < T0 → 目标持仓 0 (全平)
        """
        # 1. 获取实际持仓
        actual = self._get_actual_position(symbol)
        if actual <= self.quantity_epsilon:
            return False, Decimal('0'), "", None

        # 2. 🔥 从持仓记录获取交易所对，用于构建正确的 pair_key
        pair_map = self._get_pair_position_map(symbol)
        active_pair_key = None
        active_pair_position = None

        for pk, pp in pair_map.items():
            if pp.total_quantity > self.quantity_epsilon:
                active_pair_key = pk
                active_pair_position = pp
                break  # 同所场景应只有一个非零持仓

        if not active_pair_key or not active_pair_position:
            # 没有找到活跃持仓，降级使用 symbol 级持仓（兜底）
            position = self.positions.get(symbol)
            if not position:
                return False, Decimal('0'), "", None
            # 尝试用 position 的交易所构建 pair_key
            active_pair_key = self._build_position_key(
                symbol,
                position.exchange_buy,
                position.exchange_sell,
                position.buy_symbol or symbol,
                position.sell_symbol or symbol,
            )

        # 3. 🔥 使用持仓的 pair_key 获取方向记忆
        direction = self.open_direction.get(active_pair_key, 1)

        # 4. 🔥 使用平仓视角的价差（已经是基于实际盘口数据计算的）
        closing_spread_pct = spread_data.spread_pct
        relative_spread = self.close_relative_spread(symbol, spread_data, direction)

        # 🔥 平仓视角：使用当前实时盘口价格（来自 spread_data）
        closing_buy_exchange = spread_data.exchange_buy or ""
        closing_sell_exchange = spread_data.exchange_sell or ""
        closing_buy_symbol = spread_data.buy_symbol or symbol
        closing_sell_symbol = spread_data.sell_symbol or symbol
        closing_buy_price = spread_data.price_buy
        closing_sell_price = spread_data.price_sell

        # 🔥 开仓视角：从持仓记录中获取真实的开仓价格（加权平均）
        position = self.positions.get(symbol)
        if position and position.get_open_segments():
            # 计算加权平均开仓价格
            open_segments = position.get_open_segments()
            total_qty = sum(float(seg.open_quantity) for seg in open_segments)

            if total_qty > 0:
                # 加权平均买入价格
                avg_buy_price = sum(
                    float(seg.open_price_buy) * float(seg.open_quantity)
                    for seg in open_segments
                ) / total_qty

                # 加权平均卖出价格
                avg_sell_price = sum(
                    float(seg.open_price_sell) * float(seg.open_quantity)
                    for seg in open_segments
                ) / total_qty

                opening_buy_price = Decimal(str(avg_buy_price))
                opening_sell_price = Decimal(str(avg_sell_price))
            else:
                # 兜底：如果计算失败，使用交换价格（旧逻辑）
                opening_buy_price = closing_sell_price
                opening_sell_price = closing_buy_price

            # 交易所和交易对信息从持仓记录获取
            opening_buy_exchange = position.exchange_buy
            opening_sell_exchange = position.exchange_sell
            opening_buy_symbol = position.buy_symbol or symbol
            opening_sell_symbol = position.sell_symbol or symbol
        else:
            # 兜底：如果没有持仓记录，使用交换价格（旧逻辑）
            opening_buy_exchange = closing_sell_exchange
            opening_sell_exchange = closing_buy_exchange
            opening_buy_symbol = closing_sell_symbol
            opening_sell_symbol = closing_buy_symbol
            opening_buy_price = closing_sell_price
            opening_sell_price = closing_buy_price

        def _fmt_leg(exchange: str, sym: str, price: Optional[Decimal]) -> str:
            exch = exchange or "?"
            sym_val = sym or symbol
            if price is None:
                return f"{exch}/{sym_val}@?"
            try:
                return f"{exch}/{sym_val}@{float(price):.2f}"
            except Exception:
                return f"{exch}/{sym_val}@?"

        closing_view = (
            f"平仓视角: 买{_fmt_leg(closing_buy_exchange, closing_buy_symbol, closing_buy_price)} "
            f"→ 卖{_fmt_leg(closing_sell_exchange, closing_sell_symbol, closing_sell_price)}"
        )
        opening_view = (
            f"开仓视角: 买{_fmt_leg(opening_buy_exchange, opening_buy_symbol, opening_buy_price)} "
            f"→ 卖{_fmt_leg(opening_sell_exchange, opening_sell_symbol, opening_sell_price)}"
        )
        price_snapshot = f"{closing_view} | {opening_view}"

        logger.debug(
            f"[{symbol}] 平仓价差分析: "
            f"平仓价差={closing_spread_pct:.4f}%, "
            f"方向归一后={relative_spread:.4f}%, "
            f"{price_snapshot}"
        )

        # 4. 🔥 根据方向归一后的价差，计算目标持仓（考虑平仓阈值）
        target = self._calculate_target_position_by_spread(
            symbol,
            relative_spread,
            config
        )

        # 5. 计算需要平仓的数量
        close_delta = actual - target

        if close_delta <= self.quantity_epsilon:
            # 不需要平仓，但记录状态
            close_threshold = self._get_close_persistence_threshold(
                actual, config, symbol)
            # 🔥 使用持仓的交易所信息作为key,避免1对多模式下日志被节流
            position = self.positions.get(symbol)
            if position:
                log_key = f"{symbol}_{position.exchange_buy}_{position.exchange_sell}_close_status"
            else:
                log_key = f"{symbol}_close_status"
            # 监测类日志默认降级为 DEBUG，减少 unified_decision.log 噪音
            self._log_debug_throttle(
                log_key,
                (
                    f"🔍 [{symbol}] 平仓监测\n"
                    f"   当前价差: {closing_spread_pct:+.4f}% (归一后={relative_spread:.4f}%)\n"
                    f"   平仓阈值: ≤{close_threshold:.4f}% (T{current_grid-1})\n"
                    f"   状态: ⏳ 等待收敛\n"
                    f"   {price_snapshot}"
                ),
                interval=120  # 2分钟打印一次
            )
            self._reset_spread_persistence(f"{symbol}_close")
            return False, Decimal('0'), "", None

        # 6. 🔥 平仓持续性检查（使用对应格子的平仓阈值）
        if not skip_persistence:
            close_key = f"{symbol}_close"
            close_threshold_value = self._get_close_persistence_threshold(
                actual, config, symbol)
            if not self._check_spread_persistence(
                close_key,
                relative_spread,
                close_threshold_value,
                config,
                comparison="le"
            ):
                # 持续性未满足（或价差未达标），记录状态
                status_text = "✅ 满足条件(计时中)" if relative_spread <= close_threshold_value else "⏳ 等待收敛"
                # 🔥 使用持仓的交易所信息作为key,避免1对多模式下日志被节流
                position = self.positions.get(symbol)
                if position:
                    log_key = f"{symbol}_{position.exchange_buy}_{position.exchange_sell}_close_status"
                else:
                    log_key = f"{symbol}_close_status"
                # 监测类日志默认降级为 DEBUG，减少 unified_decision.log 噪音
                self._log_debug_throttle(
                    log_key,
                    (
                        f"🔍 [{symbol}] 平仓监测\n"
                        f"   当前价差: {closing_spread_pct:+.4f}% (归一后={relative_spread:.4f}%)\n"
                        f"   平仓阈值: ≤{close_threshold_value:.4f}% (T{current_grid-1})\n"
                        f"   状态: {status_text}\n"
                        f"   {price_snapshot}"
                    ),
                    interval=120  # 2分钟打印一次
                )
                return False, Decimal('0'), "", None

        # 7. 计算本次平仓数量（拆单）
        close_qty = self._calculate_order_quantity(
            symbol,
            close_delta,
            config,
            is_close=True,
        )
        if close_qty <= self.quantity_epsilon:
            return False, Decimal("0"), "", None

        # 🔥 平仓日志口径：显示“当前持仓段位Tn”（避免价差为负时打印成T0造成误解）
        # - 执行器侧的 “平仓Tn” 也是用持仓段位（position_segments）展示
        position_segments = self.get_current_segments(symbol)
        reason = f"网格平仓T{position_segments}(平仓价差{closing_spread_pct:.3f}%, 反转后{relative_spread:.3f}%, 目标{target}, 实际{actual})"

        close_log_key = f"{symbol}_close_grid_{position_segments}"
        self._log_info_throttle(
            close_log_key,
            (
                f"🛑 [{symbol}] {reason}, "
                f"平仓数量: {close_qty}"
            ),
            interval=self.signal_log_interval,
        )

        # 🔥 不再返回segment_id，record_close会按FIFO处理
        return True, close_qty, reason, None

    def _calculate_target_position_by_spread(
        self,
        symbol: str,
        spread_pct: float,
        config: SymbolConfig
    ) -> Decimal:
        """
        🔥 根据当前价差与配置动态计算目标持仓（兼容任意格子数）

        规则摘要：
        - `grid_config.initial_spread_threshold` = T1（首个开仓阈值）
        - `grid_config.grid_step` = 后续格子增量（Tn = T1 + (n-1)*step）
        - `T0 = T1 / 10` 作为首段平仓阈值
        - 当价差 ≥ 某个 Tn 时，允许开到第 n 格
        - 价差回落到 T(n-1) 以下时，目标持仓下调一格，实现 “开一格→回落一格才平” 的滞后逻辑
        """
        max_segments = config.grid_config.max_segments
        if max_segments <= 0:
            return Decimal('0')

        single_grid_qty = config.quantity_config.base_quantity
        if config.quantity_config.quantity_mode != "value" and single_grid_qty <= self.quantity_epsilon:
            return Decimal('0')

        open_thresholds, close_thresholds = self._build_grid_thresholds(config)
        if not open_thresholds:
            return Decimal('0')

        actual_position = self._get_actual_position(symbol)
        current_segments = 0
        if config.quantity_config.quantity_mode == "value":
            current_segments = self.get_current_segments(symbol)
        elif actual_position > self.quantity_epsilon:
            ratio = (actual_position /
                     single_grid_qty).to_integral_value(rounding=ROUND_CEILING)
            current_segments = max(0, int(ratio))

        # 价差对应可以开到的最高格子
        open_segments = self._count_segments_by_threshold(
            spread_pct, open_thresholds)
        # 价差允许继续持有的格子（平仓阈值依据 T(n-1)）
        keep_segments = self._count_segments_by_threshold(
            spread_pct, close_thresholds)
        keep_segments = min(keep_segments, current_segments)

        if open_segments > current_segments:
            target_segments = open_segments
        else:
            target_segments = keep_segments

        target_segments = min(max_segments, target_segments)
        if target_segments <= 0:
            return Decimal('0')

        if config.quantity_config.quantity_mode == "value":
            return self._quantity_after_fifo_close(symbol, Decimal(target_segments) * config.quantity_config.target_value_usdc)
        return Decimal(str(target_segments)) * single_grid_qty

    def _get_close_persistence_threshold(
        self,
        actual_position: Decimal,
        config: SymbolConfig,
        symbol: Optional[str] = None,
    ) -> float:
        """
        计算当前持仓对应的平仓持续性阈值

        用于在平仓前的持续性检查中作为比较基准，确保
        “回落到上一格” 的逻辑得到时间维度的确认。
        """
        base_qty = config.quantity_config.base_quantity
        if config.quantity_config.quantity_mode != "value" and base_qty <= self.quantity_epsilon:
            return config.grid_config.initial_spread_threshold * config.grid_config.first_close_ratio

        if config.quantity_config.quantity_mode == "value":
            current_segments = self.get_current_segments(symbol or config.symbol)
        else:
            current_segments = (actual_position / base_qty).to_integral_value(rounding=ROUND_CEILING)
        current_segments = max(
            1, min(int(current_segments), config.grid_config.max_segments))

        _, close_thresholds = self._build_grid_thresholds(config)
        if not close_thresholds:
            return 0.0

        index = min(current_segments - 1, len(close_thresholds) - 1)
        return float(close_thresholds[index])

    def _log_info_throttle(self, key: str, message: str, interval: float = 30.0):
        """
        节流打印 INFO 日志

        - 价格未变/重复信号类：120s
        - 监测类（开仓/平仓监测）：60s
        - 其他信息：默认 30s
        """
        now = time.time()
        last_time = self._log_throttle_times.get(key, 0.0)

        # 文本特征化，自动调整节流窗口
        normalized_msg = message
        long_interval = interval
        if "价格未变" in normalized_msg or "重复开仓" in normalized_msg:
            long_interval = max(long_interval, 120.0)
        elif "开仓监测" in normalized_msg or "平仓监测" in normalized_msg:
            long_interval = max(long_interval, 60.0)

        if now - last_time >= long_interval:
            logger.info(message)
            self._log_throttle_times[key] = now

    def _log_debug_throttle(self, key: str, message: str, interval: float = 30.0):
        """
        节流打印 DEBUG 日志

        说明：
        - 用于“高频但对日常运行价值不高”的监测类日志（如开仓/平仓监测、持续性通过）。
        - 复用与 INFO 相同的节流策略，确保在 DEBUG 打开时也不会刷屏。
        """
        now = time.time()
        last_time = self._log_throttle_times.get(key, 0.0)

        normalized_msg = message
        long_interval = interval
        if "价格未变" in normalized_msg or "重复开仓" in normalized_msg:
            long_interval = max(long_interval, 120.0)
        elif "开仓监测" in normalized_msg or "平仓监测" in normalized_msg:
            long_interval = max(long_interval, 60.0)

        if now - last_time >= long_interval:
            logger.debug(message)
            self._log_throttle_times[key] = now

    def _log_grid_thresholds_snapshot(self):
        """
        启动时打印一次各交易对的网格阈值（开仓/平仓）
        """
        if self._grid_thresholds_logged:
            return

        symbol_configs = dict(self.config_manager.symbol_configs)
        if self.config_manager.default_config and "__DEFAULT__" not in symbol_configs:
            symbol_configs["__DEFAULT__"] = self.config_manager.default_config

        if not symbol_configs:
            logger.info("ℹ️ [统一决策] 未找到交易对配置，跳过网格阈值打印")
            self._grid_thresholds_logged = True
            return

        logger.info("📊 [统一决策] 网格阈值表（启动仅打印一次）")
        for symbol, config in symbol_configs.items():
            open_thresholds, close_thresholds = self._build_grid_thresholds(
                config)
            if not open_thresholds:
                logger.info(f"  - {symbol}: 未配置网格阈值")
                continue

            table_lines = [
                f"  - {symbol} | base_quantity={config.quantity_config.base_quantity} | max_segments={config.grid_config.max_segments}"
            ]
            for idx, open_th in enumerate(open_thresholds):
                close_th = close_thresholds[idx] if idx < len(
                    close_thresholds) else None
                table_lines.append(
                    f"      T{idx + 1}: 开仓≥{open_th:.4f}%, 平仓<{close_th:.4f}%"
                )
            logger.info("\n".join(table_lines))

        self._grid_thresholds_logged = True

    def _build_grid_thresholds(
        self,
        config: SymbolConfig
    ) -> Tuple[list, list]:
        """
        生成开仓/平仓阈值列表

        Returns:
            (open_thresholds, close_thresholds)
            - open_thresholds[i] = 第 i+1 格的开仓阈值
            - close_thresholds[i] = 第 i+1 格的平仓阈值 (= T(i))
        """
        initial = config.grid_config.initial_spread_threshold
        step = config.grid_config.grid_step
        max_segments = config.grid_config.max_segments

        if initial <= 0 or step < 0 or max_segments <= 0:
            return [], []

        open_thresholds = []
        current = initial
        for _ in range(max_segments):
            open_thresholds.append(current)
            current += step

        # 平仓阈值：T1 → T0，Tn → T(n-1)
        # 🔥 T0 = T1 * 0.4（40%）
        t0 = initial * config.grid_config.first_close_ratio if initial > 0 else 0.0
        close_thresholds = [t0]
        close_thresholds.extend(open_thresholds[:-1])

        return open_thresholds, close_thresholds

    @staticmethod
    def _count_segments_by_threshold(
        value: float,
        thresholds: list
    ) -> int:
        """
        根据阈值列表计算满足条件的最高格子数
        """
        for idx in range(len(thresholds) - 1, -1, -1):
            if value >= thresholds[idx]:
                return idx + 1
        return 0

    def get_grid_level(self, symbol: str, spread_pct: float) -> int:
        """对外暴露的网格计算接口"""
        return max(0, self._calculate_current_grid(symbol, spread_pct))

    def get_current_segments(self, symbol: str) -> int:
        """当前持仓对应的最高网格段数"""
        position = self.positions.get(symbol)
        if not position:
            return 0

        config = self._config(symbol)
        base_qty = config.quantity_config.base_quantity
        if config.quantity_config.quantity_mode != "value" and base_qty <= self.quantity_epsilon:
            return 0

        if config.quantity_config.quantity_mode == "value":
            value = self.remaining_entry_notional(symbol)
            if value is None:
                return config.grid_config.max_segments
            # Ignore less than one common quantity step of rounding residue.
            price = max((max(seg.open_price_buy, seg.open_price_sell) for seg in position.get_open_segments()), default=Decimal("0"))
            tolerance = price * Decimal(1).scaleb(-config.quantity_config.quantity_precision)
            segments = ((max(Decimal("0"), value - tolerance)) / config.quantity_config.target_value_usdc).to_integral_value(rounding=ROUND_CEILING)
        else:
            segments = (position.total_quantity / base_qty).to_integral_value(rounding=ROUND_CEILING)
        return max(0, min(int(segments), config.grid_config.max_segments))

    def _calculate_segment_close_threshold(
        self,
        segment: PositionSegment,
        config: SymbolConfig
    ) -> float:
        """
        计算某段的固定平仓阈值（基于网格级别）

        🔥 用户最终确认的规则：

        网格定义:
        - T0 = 0.02% (initial * config.grid_config.first_close_ratio, 不开仓区间)
        - T1 = 0.05% (initial)
        - T2 = 0.08% (initial + step)
        - T3 = 0.11% (initial + 2*step)

        开仓规则:
        - 必须 >= T1 才开仓
        - 开仓 >= Tn

        平仓规则:
        - 平仓 < T(n-1) (向下偏移一个T级别)
        - T1开仓 → T0平仓 (< 0.005%)
        - T2开仓 → T1平仓 (< 0.05%)
        - T3开仓 → T2平仓 (< 0.08%)

        对称模式：平仓阈值 = T(n-1)的开仓阈值（固定）
        非对称模式：平仓阈值 = 开仓价差 - profit_per_segment（动态）
        """
        if config.grid_config.use_symmetric_close:
            # 🔥 对称平仓：固定阈值，基于grid级别
            initial = config.grid_config.initial_spread_threshold
            step = config.grid_config.grid_step

            # 🔥 根据开仓价差推断网格级别
            # 用户说明：T0 = initial*0.4 (不开仓), T1 = initial, T2 = initial+step
            open_spread = segment.open_spread_pct

            if open_spread < initial:
                # < T1，理论上不应该开仓，保护性代码
                grid = 0
            else:
                # >= T1，计算在T1之上的第几格
                # T1: initial ~ (initial+step)     → grid = 1
                # T2: (initial+step) ~ (initial+2*step) → grid = 2
                grid = int((open_spread - initial) / step) + 1

            # 🔥 平仓阈值 = T(n-1)的开仓阈值
            if grid <= 1:
                # T1开仓 → T0平仓
                # T0 = T1 * 0.4（40%）
                close_threshold = initial * config.grid_config.first_close_ratio
            else:
                # T2+开仓 → T(n-1)平仓
                # T2 (grid=2) → T1平仓 = initial = 0.05%
                # T3 (grid=3) → T2平仓 = initial + step = 0.08%
                # T4 (grid=4) → T3平仓 = initial + 2*step = 0.11%
                #
                # 公式：T(grid)平仓 = T(grid-1)开仓 = initial + (grid-2)*step
                close_threshold = initial + (grid - 2) * step
        else:
            # 非对称平仓：动态阈值，基于开仓价差
            close_threshold = segment.open_spread_pct - \
                config.grid_config.profit_per_segment

        # 确保平仓阈值 ≥ 0
        return max(0.0, close_threshold)

    # ========================================================================
    # 持仓记录管理
    # ========================================================================

    async def record_open(
        self,
        symbol: str,
        quantity: Decimal,
        spread_data: SpreadData,
        funding_rate_data: Optional[FundingRateData] = None,
        buy_order_id: Optional[str] = None,
        sell_order_id: Optional[str] = None,
        entry_price_buy: Optional[Decimal] = None,
        entry_price_sell: Optional[Decimal] = None,
        filled_quantity: Optional[Decimal] = None,
        entry_fee_buy_per_unit: Optional[Decimal] = None,
        entry_fee_sell_per_unit: Optional[Decimal] = None,
    ):
        """记录开仓"""
        if quantity <= self.quantity_epsilon:
            return

        actual_quantity = quantity if filled_quantity is None else filled_quantity
        if actual_quantity <= self.quantity_epsilon:
            return

        position = self.positions.get(symbol)
        prev_total = position.total_quantity if position else Decimal("0")
        pair_key = self._build_position_key(
            symbol,
            spread_data.exchange_buy,
            spread_data.exchange_sell,
            spread_data.buy_symbol or symbol,
            spread_data.sell_symbol or symbol,
        )
        if not position:
            position = SegmentedPosition(
                symbol=symbol,
                exchange_buy=spread_data.exchange_buy,
                exchange_sell=spread_data.exchange_sell,
                buy_symbol=spread_data.buy_symbol or symbol,
                sell_symbol=spread_data.sell_symbol or symbol,
                segments=[],
                total_quantity=Decimal('0'),
                avg_open_spread_pct=spread_data.spread_pct,
                create_time=datetime.now(),
                last_update_time=datetime.now(),
                is_open=True
            )
            self.positions[symbol] = position
        else:
            # 确保多腿信息/方向在旧持仓上也可用（方向可能随新开仓反转）
            old_buy = position.exchange_buy
            old_sell = position.exchange_sell
            position.exchange_buy = spread_data.exchange_buy or position.exchange_buy
            position.exchange_sell = spread_data.exchange_sell or position.exchange_sell
            if not position.buy_symbol or position.buy_symbol == symbol:
                position.buy_symbol = spread_data.buy_symbol or symbol
            if not position.sell_symbol or position.sell_symbol == symbol:
                position.sell_symbol = spread_data.sell_symbol or symbol

        # 创建新段（段仅用于记录）
        segment_id = position.get_next_segment_id()
        segment = PositionSegment(
            segment_id=segment_id,
            entry_fee_buy_per_unit=entry_fee_buy_per_unit,
            entry_fee_sell_per_unit=entry_fee_sell_per_unit,
            target_quantity=quantity,
            open_quantity=actual_quantity,
            open_spread_pct=spread_data.spread_pct,
            open_time=datetime.now(),
            open_price_buy=spread_data.price_buy if entry_price_buy is None else entry_price_buy,
            open_price_sell=spread_data.price_sell if entry_price_sell is None else entry_price_sell,
            open_funding_rate_buy=funding_rate_data.funding_rate_buy if funding_rate_data else 0.0,
            open_funding_rate_sell=funding_rate_data.funding_rate_sell if funding_rate_data else 0.0,
            buy_order_id=buy_order_id,
            sell_order_id=sell_order_id,
            is_closed=False
        )

        position.segments.append(segment)
        position.total_quantity += actual_quantity
        position.avg_open_spread_pct = position.calculate_avg_spread()
        position.last_update_time = datetime.now()

        should_init_memory = (
            pair_key not in self.open_direction
            or prev_total <= self.quantity_epsilon
        )
        if should_init_memory:
            direction_flag = 1 if self._config(symbol).grid_config.basis_config.get("enabled", False) or spread_data.spread_pct >= 0 else -1
            self.open_direction[pair_key] = direction_flag
            logger.info(
                "🧠 [%s] 记忆已建立 | 方向=%s | 交易所=%s→%s",
                pair_key,
                "正" if direction_flag > 0 else "负",
                spread_data.exchange_buy or "?",
                spread_data.exchange_sell or "?",
            )

        # 🔥 优化日志格式，提高可读性
        logger.info(
            f"✅ [{symbol}] 记录开仓 (段{segment_id})\n"
            f"   数量: 目标={quantity} 实际={actual_quantity}\n"
            f"   价差: {spread_data.spread_pct:.3f}%\n"
            f"   总持仓: {position.total_quantity}"
        )

        # 记录套利对级别的持仓
        self._record_pair_open(
            symbol=symbol,
            quantity=quantity,
            spread_data=spread_data,
            funding_rate_data=funding_rate_data,
            buy_order_id=buy_order_id,
            sell_order_id=sell_order_id,
            entry_price_buy=entry_price_buy,
            entry_price_sell=entry_price_sell,
            actual_quantity=actual_quantity,
        )

    def report_open_shortfall(
        self,
        symbol: str,
        requested_quantity: Decimal,
        actual_quantity: Decimal
    ) -> None:
        """记录未能成交的短缺数量，供下次开仓补齐"""
        diff = requested_quantity - actual_quantity
        if diff > self.quantity_epsilon:
            self.pending_open_shortfall[symbol] = diff
            logger.warning(
                f"⚠️ [{symbol}] 记录拆单短缺: 目标{requested_quantity}, 实际{actual_quantity}, "
                f"缺口{diff}"
            )
        else:
            self.pending_open_shortfall[symbol] = Decimal('0')

    async def record_close(
        self,
        symbol: str,
        quantity: Decimal,
        spread_data: SpreadData,
        reason: str,
        segment_id: Optional[int] = None  # 保留参数兼容性，但不再使用
    ):
        """
        记录平仓（按FIFO）

        🔥 V2简化版：总量驱动，不关心具体平哪个段
        - 按FIFO顺序平仓（用于统计盈亏）
        - segment_id参数保留但不使用
        """
        if quantity <= self.quantity_epsilon:
            return

        position = self.positions.get(symbol)
        if not position:
            return

        closed_segments = self._apply_close_to_position(
            position, quantity, spread_data)

        if position.total_quantity <= self.quantity_epsilon:
            self._unfavorable_funding.pop(symbol, None)
            getattr(self, "close_blocked_reasons", {}).pop(symbol, None)
            position.total_quantity = Decimal("0")
            position.is_open = False
            pair_key = self._build_position_key(
                symbol,
                spread_data.exchange_buy,
                spread_data.exchange_sell,
                spread_data.buy_symbol or symbol,
                spread_data.sell_symbol or symbol,
            )
            if self.open_direction.pop(pair_key, None) is not None:
                logger.info("🧠 [%s] 记忆已清除（持仓归零）", pair_key)
            if self.scalping_active.get(symbol, False):
                self.scalping_active[symbol] = False
                logger.info(f"🟢 [{symbol}] 剥头皮模式退出，恢复网格模式")

        # 🔥 优化日志格式，提高可读性
        logger.info(
            f"🛑 [{symbol}] 记录平仓\n"
            f"   数量: {quantity}\n"
            f"   关闭段: {closed_segments}\n"
            f"   剩余持仓: {position.total_quantity}\n"
            f"   原因: {reason}"
        )

        # 同步更新套利对级别持仓
        self._record_pair_close(symbol, quantity, spread_data)
        # 🔥 完全平仓后，清理持仓与记忆（positions/pair_positions/短缺/价差记忆等）
        self._cleanup_position_state(symbol)

    # ========================================================================
    # 辅助方法
    # ========================================================================

    def iter_pair_positions(self) -> List[Tuple[str, SegmentedPosition]]:
        """返回所有套利对级别的持仓列表"""
        snapshot: List[Tuple[str, SegmentedPosition]] = []
        for symbol, pair_map in self.pair_positions.items():
            for pair_position in pair_map.values():
                snapshot.append((symbol, pair_position))
        return snapshot

    def _build_position_key(
        self,
        symbol: str,
        exchange_buy: Optional[str],
        exchange_sell: Optional[str],
        buy_symbol: Optional[str],
        sell_symbol: Optional[str],
    ) -> str:
        symbol_key = (symbol or "").upper()
        buy_exchange = (exchange_buy or "").lower()
        sell_exchange = (exchange_sell or "").lower()
        buy_sym = (buy_symbol or symbol).upper()
        sell_sym = (sell_symbol or symbol).upper()
        return f"{symbol_key}:{buy_exchange}->{sell_exchange}:{buy_sym}->{sell_sym}"

    def _is_same_price(
        self,
        previous: Optional[Decimal],
        current: Optional[Decimal],
    ) -> bool:
        if previous is None or current is None:
            return False
        return abs(previous - current) <= self.price_epsilon

    def _get_pair_position_map(self, symbol: str) -> Dict[str, SegmentedPosition]:
        return self.pair_positions.setdefault(symbol, {})

    def _record_pair_open(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        spread_data: SpreadData,
        funding_rate_data: Optional[FundingRateData],
        buy_order_id: Optional[str],
        sell_order_id: Optional[str],
        entry_price_buy: Optional[Decimal],
        entry_price_sell: Optional[Decimal],
        actual_quantity: Decimal,
    ) -> None:
        pair_key = self._build_position_key(
            symbol,
            spread_data.exchange_buy,
            spread_data.exchange_sell,
            spread_data.buy_symbol or symbol,
            spread_data.sell_symbol or symbol,
        )
        pair_map = self._get_pair_position_map(symbol)
        pair_position = pair_map.get(pair_key)
        if not pair_position:
            pair_position = SegmentedPosition(
                symbol=symbol,
                exchange_buy=spread_data.exchange_buy,
                exchange_sell=spread_data.exchange_sell,
                buy_symbol=spread_data.buy_symbol or symbol,
                sell_symbol=spread_data.sell_symbol or symbol,
                segments=[],
                total_quantity=Decimal("0"),
                avg_open_spread_pct=spread_data.spread_pct,
                create_time=datetime.now(),
                last_update_time=datetime.now(),
                is_open=True,
                pair_key=pair_key,
            )
            pair_map[pair_key] = pair_position

        segment = PositionSegment(
            segment_id=pair_position.get_next_segment_id(),
            target_quantity=quantity,
            open_quantity=actual_quantity,
            open_spread_pct=spread_data.spread_pct,
            open_time=datetime.now(),
            open_price_buy=spread_data.price_buy if entry_price_buy is None else entry_price_buy,
            open_price_sell=spread_data.price_sell if entry_price_sell is None else entry_price_sell,
            open_funding_rate_buy=funding_rate_data.funding_rate_buy if funding_rate_data else 0.0,
            open_funding_rate_sell=funding_rate_data.funding_rate_sell if funding_rate_data else 0.0,
            buy_order_id=buy_order_id,
            sell_order_id=sell_order_id,
            is_closed=False,
        )
        pair_position.segments.append(segment)
        pair_position.total_quantity += actual_quantity
        pair_position.avg_open_spread_pct = pair_position.calculate_avg_spread()
        pair_position.last_update_time = datetime.now()
        pair_position.is_open = True

    def _apply_close_to_position(
        self,
        position: SegmentedPosition,
        quantity: Decimal,
        spread_data: SpreadData,
    ) -> List[int]:
        remaining = quantity
        closed_segments: List[int] = []

        for segment in position.segments:
            if segment.is_closed or segment.open_quantity <= self.quantity_epsilon:
                continue
            if remaining <= self.quantity_epsilon:
                break
            close_this = min(remaining, segment.open_quantity)
            segment.open_quantity -= close_this
            remaining -= close_this
            if segment.open_quantity <= self.quantity_epsilon:
                segment.open_quantity = Decimal("0")
                segment.is_closed = True
                segment.close_time = datetime.now()
                segment.close_spread_pct = spread_data.spread_pct
                segment.close_price_buy = spread_data.price_buy
                segment.close_price_sell = spread_data.price_sell
                closed_segments.append(segment.segment_id)

        position.total_quantity -= quantity
        if position.total_quantity < Decimal("0"):
            position.total_quantity = Decimal("0")
        position.avg_open_spread_pct = position.calculate_avg_spread()
        position.last_update_time = datetime.now()

        if position.total_quantity <= self.quantity_epsilon:
            position.total_quantity = Decimal("0")
            position.is_open = False

        return closed_segments

    def _record_pair_close(
        self,
        symbol: str,
        quantity: Decimal,
        spread_data: SpreadData,
    ) -> None:
        pair_map = self.pair_positions.get(symbol)
        if not pair_map:
            return

        # 🔍 优先在现有套利对里按“交易所集合”匹配（忽略方向），避免平仓视角反转导致找不到记录
        closing_exchanges = {
            (spread_data.exchange_sell or "").lower(),
            (spread_data.exchange_buy or "").lower(),
        }
        pair_position = None
        for _, pos in pair_map.items():
            existing_exchanges = {
                (pos.exchange_buy or "").lower(),
                (pos.exchange_sell or "").lower(),
            }
            if existing_exchanges == closing_exchanges:
                pair_position = pos
                break

        # 兜底：按原有方向key尝试获取（兼容旧逻辑）
        if not pair_position:
            pair_key = self._build_position_key(
                symbol,
                spread_data.exchange_sell,
                spread_data.exchange_buy,
                spread_data.sell_symbol or symbol,
                spread_data.buy_symbol or symbol,
            )
            pair_position = pair_map.get(pair_key)
            if not pair_position:
                return

        adjust_qty = min(quantity, pair_position.total_quantity)
        self._apply_close_to_position(pair_position, adjust_qty, spread_data)

        if pair_position.total_quantity <= self.quantity_epsilon:
            pair_position.total_quantity = Decimal("0")
            pair_position.is_open = False

    def _cleanup_position_state(self, symbol: str) -> None:
        """
        持仓归零后清理所有相关状态，避免UI和记忆残留。
        """
        # 清理 symbol 级持仓与方向记忆
        if symbol in self.positions and self.positions[symbol].total_quantity <= self.quantity_epsilon:
            self.positions.pop(symbol, None)
        self.pending_open_shortfall.pop(symbol, None)
        self.scalping_active.pop(symbol, None)
        self._spread_persistence_state.pop(symbol, None)
        self._last_open_signal_prices.pop(symbol, None)

        # 清理套利对级别持仓
        pair_map = self.pair_positions.get(symbol)
        if pair_map:
            # 清除已归零 pair 的方向记忆
            for key, v in list(pair_map.items()):
                if v.total_quantity <= self.quantity_epsilon:
                    if self.open_direction.pop(key, None) is not None:
                        logger.info(f"🧠 [{key}] 记忆已清除（套利对持仓归零）")
            to_delete = [k for k, v in pair_map.items(
            ) if v.total_quantity <= self.quantity_epsilon]
            for key in to_delete:
                pair_map.pop(key, None)
            if not pair_map:
                self.pair_positions.pop(symbol, None)

    def _check_spread_persistence(
        self,
        symbol: str,
        spread_pct: float,
        threshold: float,
        config: SymbolConfig,
        comparison: str = "ge"
    ) -> bool:
        """
        检查价差持续性（连续N秒满足条件）

        Args:
            symbol: 交易对（或 special_key，例如 symbol_close）
            spread_pct: 当前价差
            threshold: 对比阈值
            config: 交易对配置
            comparison: 比较方式（ge = >=, le = <=）
        """
        required_seconds = config.grid_config.spread_persistence_seconds
        if required_seconds <= 1:
            self._spread_persistence_state.pop(symbol, None)
            return self._compare_spread(spread_pct, threshold, comparison)

        signature = (threshold, comparison, required_seconds)
        previous = self._spread_persistence_state.get(symbol)
        if previous is not None and previous.get("threshold_signature") != signature:
            self._reset_spread_persistence(symbol)
        strict_mode = config.grid_config.strict_persistence_check
        state = self._spread_persistence_state.setdefault(
            symbol,
            {
                'last_bucket': None,
                'count': 0,
                'pass_logged_this_second': False,
                'strict_window_start': None,
                'strict_pass_logged_bucket': None,
                'strict_has_passed': False
            }
        )

        state["threshold_signature"] = signature
        if strict_mode:
            return self._check_strict_persistence_internal(
                symbol=symbol,
                spread_pct=spread_pct,
                threshold=threshold,
                required_seconds=required_seconds,
                comparison=comparison,
                state=state
            )

        return self._check_relaxed_persistence_internal(
            symbol=symbol,
            spread_pct=spread_pct,
            threshold=threshold,
            required_seconds=required_seconds,
            comparison=comparison,
            state=state
        )

    def _check_relaxed_persistence_internal(
        self,
        symbol: str,
        spread_pct: float,
        threshold: float,
        required_seconds: int,
        comparison: str,
        state: Dict
    ) -> bool:
        """宽松模式：每秒至少一次满足条件"""
        if not self._compare_spread(spread_pct, threshold, comparison):
            self._reset_spread_persistence(symbol)
            return False

        current_bucket = int(time.time())
        last_bucket = state.get('last_bucket')

        if last_bucket is None:
            state['count'] = 1
            state['pass_logged_this_second'] = False
            logger.info(
                f"🟢 [{symbol}] 持续性检查开始(宽松) - "
                f"需连续{required_seconds}秒, 进度: 1/{required_seconds}"
            )
        elif current_bucket == last_bucket:
            # 同一秒内 - 不增加计数，避免日志刷屏
            pass
        elif current_bucket == last_bucket + 1:
            state['count'] += 1
            state['pass_logged_this_second'] = False
        else:
            gap = current_bucket - last_bucket
            logger.warning(
                f"⚠️  [{symbol}] 持续性中断(宽松) - "
                f"时间间隔{gap}秒 > 1秒, "
                f"进度{state['count']}秒被重置"
            )
            state['count'] = 1
            state['pass_logged_this_second'] = False

        state['last_bucket'] = current_bucket

        if state['count'] < required_seconds:
            return False

        if not state.get('pass_logged_this_second', False):
            # 持续性“通过”属于高频状态变化，默认降级为 DEBUG，避免 unified_decision.log 刷屏
            logger.debug(
                f"🎉 [{symbol}] 持续性通过(宽松) - "
                f"已连续{state['count']}秒, 允许交易"
            )
            state['pass_logged_this_second'] = True

        return True

    def _check_strict_persistence_internal(
        self,
        symbol: str,
        spread_pct: float,
        threshold: float,
        required_seconds: int,
        comparison: str,
        state: Dict
    ) -> bool:
        """严格模式：连续N秒内所有采样都必须满足条件"""
        meets_condition = self._compare_spread(
            spread_pct, threshold, comparison)
        now = time.time()

        if not meets_condition:
            if state.get('strict_window_start') is not None:
                # 🔥 改为DEBUG级别，减少WARNING日志量
                logger.debug(
                    f"⚠️  [{symbol}] 持续性中断(严格) - 样本未达阈值, 计时清零"
                )
            state['strict_window_start'] = None
            state['strict_pass_logged_bucket'] = None
            state['strict_has_passed'] = False
            return False

        if state.get('strict_window_start') is None:
            state['strict_window_start'] = now
            state['strict_pass_logged_bucket'] = None
            state['strict_has_passed'] = False
            # 🔥 改为DEBUG级别，减少INFO日志量
            logger.debug(
                f"🟢 [{symbol}] 持续性检查开始(严格) - "
                f"需连续{required_seconds}秒, 正在计时"
            )

        elapsed = now - state['strict_window_start']
        if elapsed >= required_seconds:
            current_bucket = int(now)
            if not state.get('strict_has_passed'):
                # 持续性“通过”属于高频状态变化，默认降级为 DEBUG，避免 unified_decision.log 刷屏
                logger.debug(
                    f"🎉 [{symbol}] 持续性通过(严格) - "
                    f"已连续{required_seconds}秒, 允许交易"
                )
                state['strict_pass_logged_bucket'] = current_bucket
                state['strict_has_passed'] = True
            return True

        return False

    def _reset_spread_persistence(self, symbol: str):
        """重置价差持续性状态"""
        state = self._spread_persistence_state.get(symbol)
        if state:
            count = state.get('count', 0)
            strict_active = state.get('strict_window_start') is not None
            if count > 0 or strict_active:
                mode_hint = "严格" if strict_active else "宽松"
                # 🔥 改为DEBUG级别，减少WARNING日志量
                logger.debug(
                    f"🔄 [{symbol}] 持续性重置({mode_hint}) - "
                    f"进度已被清零 (价差不满足)"
                )
        self._spread_persistence_state.pop(symbol, None)

    def _build_persistence_key(
        self,
        symbol: str,
        spread_data: Optional[SpreadData]
    ) -> str:
        """
        为价差持续性生成唯一key，避免同一symbol下不同交易所组合互相干扰。
        """
        if not spread_data:
            return symbol

        buy = (spread_data.exchange_buy or "").strip().lower()
        sell = (spread_data.exchange_sell or "").strip().lower()
        if buy and sell:
            return f"{symbol}_{buy}_{sell}"
        if buy:
            return f"{symbol}_{buy}"
        if sell:
            return f"{symbol}_{sell}"
        return symbol

    @staticmethod
    def _compare_spread(value: float, threshold: float, comparison: str) -> bool:
        if comparison == "le":
            return value <= threshold
        return value >= threshold

    def _calculate_profit(self, symbol: str, current_spread_pct: float) -> float:
        """计算当前盈利百分比"""
        position = self.positions.get(symbol)
        if not position:
            return 0.0

        avg_entry_spread = position.calculate_avg_spread()
        return avg_entry_spread - current_spread_pct

    def _get_current_price(self, symbol: str) -> Decimal:
        """获取当前价格（用于按金额模式）"""
        position = self.positions.get(symbol)
        if not position or not position.segments:
            return Decimal('0')

        # 使用最新段的买入价格作为参考
        latest_segment = max(position.segments, key=lambda s: s.segment_id)
        return latest_segment.open_price_buy or Decimal('0')

    def _format_quantity(self, quantity: Decimal, precision: int) -> Decimal:
        """格式化数量精度"""
        if precision <= 0:
            return quantity.quantize(Decimal('1'), rounding=ROUND_FLOOR)

        quantizer = Decimal('0.1') ** precision
        return quantity.quantize(quantizer, rounding=ROUND_FLOOR)

    def get_position(self, symbol: str) -> Optional[SegmentedPosition]:
        """获取持仓信息"""
        return self.positions.get(symbol)

    def is_scalping_active(self, symbol: str) -> bool:
        """检查剥头皮是否激活"""
        return self.scalping_active.get(symbol, False)

    def _calculate_net_spread(self, spread_data):
        return spread_data.spread_pct - self._round_trip_fee_pct(spread_data)

    def _round_trip_fee_pct(self, spread_data):
        if not self.deduct_fees:
            return 0.0
        return sum(_round_trip_fee_breakdown(spread_data, self.exchange_order_modes, self.exchange_fee_config))
