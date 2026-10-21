"""Validate stock strategy/execution parity before credentials or connections."""
from datetime import date, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
import yaml


def positive(value, label, *, zero=False):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or (number < 0 if zero else number <= 0):
            raise ValueError()
        return number
    except (ValueError, TypeError, InvalidOperation):
        raise ValueError(f"Invalid {label}") from None


STOCK_COUNTER_VENUES = ('arcus', 'lighter_rh')


def stock_venues(order_modes):
    """Stock pair = Lighter mainnet + exactly one counter venue (Arcus legacy or Lighter RH)."""
    venues = set(order_modes or {}) - {'default'}
    if len(venues) != 2 or 'lighter' not in venues or not (venues - {'lighter'}) <= set(STOCK_COUNTER_VENUES):
        raise ValueError('Stock profile requires lighter + one of ' + '/'.join(STOCK_COUNTER_VENUES))
    return venues


def validate_stock_order_modes(order_modes, venues):
    """market/market, or maker->taker: exactly one post-only maker limit leg + one market hedge leg."""
    modes = {venue: (order_modes.get(venue) or {}) for venue in venues}
    kinds = sorted(item.get('order_mode') for item in modes.values())
    if kinds == ['market', 'market']:
        return
    if kinds != ['limit', 'market']:
        raise ValueError('Stock protected execution supports market/market or maker(limit)+taker(market) only')
    maker = next(item for item in modes.values() if item.get('order_mode') == 'limit')
    taker = next(item for item in modes.values() if item.get('order_mode') == 'market')
    if not maker.get('maker_mode') or not maker.get('post_only'):
        raise ValueError('Stock limit leg must be a post-only maker (maker_mode: true, post_only: true)')
    if maker.get('force_sequential_limit') or taker.get('force_limit_when_second_leg'):
        raise ValueError('Maker->taker forbids sequential/aggressive limit overrides')
    for key, upper in (('maker_cancel_adverse_pct', 1), ('hedge_max_slippage_pct', 1)):
        if key in maker and not 0 <= positive(maker[key], key, zero=True) < upper:
            raise ValueError('Invalid ' + key)
    if 'maker_poll_interval_seconds' in maker:
        positive(maker['maker_poll_interval_seconds'], 'maker poll interval')


def merge_stock_grid(default, specific):
    merged = {**default, **specific}
    for key in ("basis_config", "stock_session"):
        merged[key] = {**default.get(key, {}), **specific.get(key, {})}
    return merged


def validate_signal_settings(grid):
    basis = grid['basis_config']
    for key in ('ema_seconds', 'warmup_seconds', 'drift_seconds', 'max_drift_pct', 'max_gap_seconds', 'funding_horizon_hours', 'funding_max_age_seconds', 'recovery_seconds', 'reset_after_seconds', 'checkpoint_interval_seconds'):
        positive(basis[key], key)
    if basis['warmup_seconds'] < basis['drift_seconds']:
        raise ValueError('Basis warmup must cover drift history')
    if not basis['max_gap_seconds'] < basis['recovery_seconds'] <= basis['reset_after_seconds'] or basis['checkpoint_interval_seconds'] > basis['reset_after_seconds']:
        raise ValueError('Invalid basis recovery/checkpoint intervals')
    schedule = grid['stock_session']
    if 'enabled' in schedule and not isinstance(schedule['enabled'], bool):
        raise ValueError('stock_session.enabled must be a boolean')
    positive(schedule['offhours_threshold_pct'], 'offhours threshold')
    years = schedule.get('calendar_valid_years', [])
    if not isinstance(years, list) or not all(isinstance(year, int) and not isinstance(year, bool) and 1 <= year <= 9999 for year in years):
        raise ValueError('Invalid calendar years')
    for value in schedule.get('closed_dates', []) + schedule.get('closed_overrides', []):
        date.fromisoformat(value)
    for day, end in schedule.get('early_closes', {}).items():
        date.fromisoformat(day)
        if not time(9, 30) < time.fromisoformat(end) <= time(16):
            raise ValueError('Invalid early close')


def validate_stock_profiles(segmented_path, execution_path):
    strategy = yaml.safe_load(Path(segmented_path).read_text())
    execution = yaml.safe_load(Path(execution_path).read_text())
    default = strategy.get('default_config', {})
    grid = default.get('grid_config', {})
    if not strategy.get("system_mode", {}).get("stock_profile", False):
        return  # Existing fixed profiles retain their contract.
    settings = execution['arbitrage_execution']
    fees = strategy['decision']['exchange_fee_config']
    if fees != settings['exchange_fees']:
        raise ValueError('Decision/execution fees differ')
    if strategy['decision'].get('exchange_order_modes') != settings['exchange_order_modes']:
        raise ValueError('Decision/execution order modes differ')
    venues = stock_venues(settings['exchange_order_modes'])
    if set(fees) - {'default'} != venues:
        raise ValueError('Fee config must cover exactly the stock venues')
    validate_stock_order_modes(settings['exchange_order_modes'], venues)
    for exchange, rates in fees.items():
        for name in ('limit_fee_rate', 'market_fee_rate'):
            positive(rates[name], exchange + name, zero=True)
    symbols = {key: value for key, value in strategy['symbol_configs'].items() if value.get('enabled', True)}
    execution_symbols = set(settings['quantity_config']) - {'default'}
    if set(symbols) != execution_symbols:
        raise ValueError('Decision/execution symbol sets differ')
    for symbol, specific in [('default', default), *symbols.items()]:
        quantity = {**default['quantity_config'], **specific.get('quantity_config', {})}
        local_grid = merge_stock_grid(grid, specific.get('grid_config', {}))
        validate_signal_settings(local_grid)
        risk = {**default['risk_config'], **specific.get('risk_config', {})}
        actual = settings['quantity_config'][symbol]
        if quantity['quantity_mode'] != 'value' or actual.get('quantity_mode') != 'value':
            raise ValueError('Stock quantities must use value mode')
        for wanted, provided in [('target_value_usdc', 'target_value_usdc'), ('split_order_amount_usdc', 'single_order_amount_usdc')]:
            if positive(quantity[wanted], wanted) != positive(actual[provided], provided):
                raise ValueError(f'{symbol}: quantity amounts differ')
        if positive(risk['max_position_value'], 'max_position_value') != positive(actual['max_position_usdc'], 'max_position_usdc'):
            raise ValueError(f'{symbol}: position value caps differ')
        if quantity['quantity_precision'] != actual['quantity_precision'] or not isinstance(actual['quantity_precision'], int) or not 0 <= actual['quantity_precision'] <= 12:
            raise ValueError(f'{symbol}: quantity precision differs or invalid')
        if 'single_order_quantity' in actual or 'max_position_quantity' in actual:
            raise ValueError('Value mode cannot carry fixed quantity overrides')
        positive(risk['max_loss_percent'], 'max loss')
        positive(risk['funding_rate_diff_threshold'], 'funding threshold', zero=True)
        positive(risk['max_unfavorable_funding_hours'], 'max funding hours')
        if int(risk['max_unfavorable_funding_hours']) != risk['max_unfavorable_funding_hours']:
            raise ValueError('Funding hours must be an integer')
        if local_grid.get('scalping_enabled', False):
            raise ValueError('Stock basis mode requires FIFO grid closes; scalping is unsupported')
        if positive(quantity['split_order_amount_usdc'], 'split amount') > positive(quantity['target_value_usdc'], 'grid amount'):
            raise ValueError('Split amount exceeds grid amount')
        if not 0 < positive(local_grid['first_close_ratio'], 'first close ratio') < 1:
            raise ValueError('first_close_ratio must be between 0 and 1')
        for key in ('initial_spread_threshold', 'grid_step', 'max_segments', 'orderbook_aggregate_levels', 'price_stability_window_seconds', 'price_stability_threshold_pct', 'spread_persistence_seconds'):
            positive(local_grid[key], key)
        if int(local_grid['max_segments']) != local_grid['max_segments'] or int(local_grid['orderbook_aggregate_levels']) != local_grid['orderbook_aggregate_levels']:
            raise ValueError('Segment and depth counts must be integers')
        if positive(local_grid['slippage_tolerance'], 'slippage') >= 1:
            raise ValueError('slippage must be less than one')
    balances = strategy['risk_control']['balance_management']
    for key in ('min_balance_warning', 'min_balance_close_position', 'check_interval', 'equity_gap_alert_threshold_usdc', 'equity_gap_alert_interval_seconds'):
        positive(balances[key], key)
    guard = strategy['risk_control']['open_only_close']
    positive(guard['low_balance']['min_equity_usdc'], 'opening equity')
    positive(guard['max_exchange_position_value']['max_value_usdc'], 'exchange cap')
    positive(guard['dynamic_max_exchange_position_value']['equity_multiplier'], 'equity multiplier')
    volatility = guard['price_volatility']
    for key in ('threshold_pct', 'window_seconds', 'block_seconds'):
        positive(volatility[key], key)
    constraints_by_symbol = strategy.get('market_constraints', {})
    if set(constraints_by_symbol) != {name.split('-')[0] for name in symbols}:
        raise ValueError('Market constraints must cover exactly the active stock symbols')
    for symbol, constraints in constraints_by_symbol.items():
        if set(constraints.get('min_notional_by_exchange', {})) != venues:
            raise ValueError('Market constraints require both venue minimum notionals')
        step = positive(constraints['quantity_step'], 'quantity step')
        positive(constraints['min_quantity'], 'minimum quantity')
        for value in constraints['min_notional_by_exchange'].values():
            positive(value, 'minimum notional')
        match = next((value for key, value in symbols.items() if key.split('-')[0] == symbol), None)
        if match is None:
            raise ValueError('Market constraints include an inactive symbol')
        precision = match['quantity_config'].get('quantity_precision', default['quantity_config']['quantity_precision'])
        if step != Decimal(1).scaleb(-precision):
            raise ValueError('Common step and quantity precision differ')
