"""Lighter mainnet <-> Lighter Robinhood Chain (maker->taker) offline contracts. Never contacts an exchange."""
import asyncio
import copy
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from core.adapters.exchanges.adapters.lighter import LighterAdapter
from core.adapters.exchanges.adapters.lighter_rh import LighterRHAdapter
from core.adapters.exchanges.factory import ExchangeFactory
from core.adapters.exchanges.interface import ExchangeConfig
from core.adapters.exchanges.models import ExchangeType, OrderData, OrderSide, OrderStatus, OrderType
from core.adapters.exchanges.venues import is_lighter_family
from core.services.arbitrage_monitor_v2.config.arbitrage_config import ExchangeOrderModeConfig
from core.services.arbitrage_monitor_v2.config.stock_preflight import validate_stock_profiles
from core.services.arbitrage_monitor_v2.execution.arbitrage_executor import (
    ArbitrageExecutor, ExecutionConfig, ExecutionRequest,
)

D = Decimal
SYMBOL = 'MU-USDC-PERP'
SEGMENTED = Path('config/arbitrage/arbitrage_segmented_rh.yaml')
EXECUTION = Path('config/arbitrage/arbitrage_lighter_rh.yaml')


def run(coro):
    return asyncio.run(coro)


def cfg(name):
    return ExchangeConfig(name, name, ExchangeType.PERPETUAL, '', '')


def test_rh_adapter_identity_endpoints_and_chain():
    rh = LighterRHAdapter(cfg('lighter_rh'))
    main = LighterAdapter(cfg('lighter'))
    assert rh._base.base_url == 'https://api.rh.lighter.xyz'
    assert rh._base.ws_url == 'wss://api.rh.lighter.xyz/stream'
    assert rh._base.chain_id == 466324 and rh._base.env_prefix == 'LIGHTER_RH'
    assert rh._rest.exchange_id == 'lighter_rh'
    assert 'api.rh.lighter' not in main._base.base_url and main._base.env_prefix == 'LIGHTER'
    assert rh._base.client_order_id_offset != main._base.client_order_id_offset
    assert is_lighter_family('lighter_rh') and is_lighter_family('lighter') and not is_lighter_family('arcus')


def test_rh_credentials_never_fall_back_to_mainnet_env(monkeypatch):
    monkeypatch.setenv('LIGHTER_API_KEY_PRIVATE_KEY', 'mainnet-only')
    monkeypatch.setenv('LIGHTER_ACCOUNT_INDEX', '11')
    monkeypatch.delenv('LIGHTER_RH_API_KEY_PRIVATE_KEY', raising=False)
    monkeypatch.delenv('LIGHTER_RH_ACCOUNT_INDEX', raising=False)
    rh = LighterRHAdapter(cfg('lighter_rh'))
    assert getattr(rh._base, 'api_key_private_key', None) != 'mainnet-only'
    assert str(getattr(rh._base, 'account_index', '')) != '11'


def test_factory_creates_rh_adapter():
    adapter = ExchangeFactory().create_adapter(exchange_id='lighter_rh', config=cfg('lighter_rh'))
    assert isinstance(adapter, LighterRHAdapter)


def test_rh_profiles_pass_preflight_and_legacy_still_passes():
    validate_stock_profiles(SEGMENTED, EXECUTION)
    validate_stock_profiles('config/arbitrage/arbitrage_segmented.yaml', 'config/arbitrage/arbitrage_lighter_arcus.yaml')


def _mutated(tmp_path, mutate):
    strategy = yaml.safe_load(SEGMENTED.read_text())
    execution = yaml.safe_load(EXECUTION.read_text())
    mutate(strategy, execution)
    a, b = tmp_path / 's.yaml', tmp_path / 'x.yaml'
    a.write_text(yaml.safe_dump(strategy)); b.write_text(yaml.safe_dump(execution))
    return a, b


def _both(key, value):
    def mutate(strategy, execution):
        for modes in (strategy['decision']['exchange_order_modes'], execution['arbitrage_execution']['exchange_order_modes']):
            modes['lighter_rh'][key] = value
    return mutate


@pytest.mark.parametrize('mutate', [
    _both('post_only', False),
    _both('maker_mode', False),
    _both('force_sequential_limit', True),
    lambda s, x: [m['lighter'].update(order_mode='limit') for m in (s['decision']['exchange_order_modes'], x['arbitrage_execution']['exchange_order_modes'])],
    lambda s, x: x['arbitrage_execution']['exchange_order_modes']['lighter_rh'].update(post_only=False),
    lambda s, x: s['market_constraints']['MU']['min_notional_by_exchange'].pop('lighter_rh'),
    lambda s, x: [m.update(arcus=copy.deepcopy(m['lighter'])) for m in (s['decision']['exchange_order_modes'], x['arbitrage_execution']['exchange_order_modes'])],
])
def test_preflight_rejects_unsafe_rh_profiles(tmp_path, mutate):
    with pytest.raises(ValueError):
        validate_stock_profiles(*_mutated(tmp_path, mutate))


def test_rh_minimums_cover_both_venues():
    strategy = yaml.safe_load(SEGMENTED.read_text())
    rh_min = {'MU': D('0.01'), 'SNDK': D('0.01'), 'AMD': D('0.03'), 'NVDA': D('0.04'), 'INTC': D('0.05'), 'SPCX': D('0.04'),
              'BTC': D('0.0002'), 'ETH': D('0.005'), 'SOL': D('0.1'), 'HYPE': D('0.1')}
    for name, item in strategy['symbol_configs'].items():
        mins = item['quantity_config']['min_exchange_order_qty']
        base = name.split('-')[0]
        assert D(str(mins['lighter_rh'])) >= rh_min[base]
        assert D(str(mins['default'])) >= max(D(str(mins['lighter'])), rh_min[base])
        assert D(strategy['market_constraints'][base]['min_quantity']) >= rh_min[base]


def test_basis_observation_pairs_lighter_with_rh():
    from core.services.arbitrage_monitor_v2.decision import unified_decision_engine as module
    source = Path(module.__file__).read_text()
    assert '"lighter", counter' in source


def test_symbol_conversion_supports_rh():
    from core.services.arbitrage_monitor.utils.symbol_converter import SimpleSymbolConverter
    converter = SimpleSymbolConverter()
    assert converter.convert_to_exchange(SYMBOL, 'lighter_rh') == 'MU'
    assert converter.convert_from_exchange('NVDA', 'lighter_rh') == 'NVDA-USDC-PERP'


# ---------------------------------------------------------------- maker -> taker execution

def book(bid, ask):
    return SimpleNamespace(best_bid=SimpleNamespace(price=D(bid), size=D('10')),
                           best_ask=SimpleNamespace(price=D(ask), size=D('10')))


def order(status=OrderStatus.OPEN, filled='0', amount='0.1', price='99.98', oid='M1'):
    return OrderData(id=oid, client_id=oid, symbol=SYMBOL, side=OrderSide.BUY, type=OrderType.LIMIT,
                     amount=D(amount), price=D(price), filled=D(filled), remaining=D(amount) - D(filled),
                     cost=D('0'), average=None, status=status, timestamp=datetime.now(), updated=None,
                     fee=None, trades=[], params={}, raw_data={})


def maker_executor(books, rh_orders, **maker):
    rh = SimpleNamespace(create_order=AsyncMock(return_value=order()),
                         get_order=AsyncMock(side_effect=rh_orders),
                         cancel_order=AsyncMock(return_value=order(status=OrderStatus.CANCELED)),
                         strict_order_state=True)
    main = SimpleNamespace(create_order=AsyncMock(return_value=None), strict_order_state=True)
    modes = {
        'default': ExchangeOrderModeConfig(order_mode='market'),
        'lighter': ExchangeOrderModeConfig(order_mode='market', priority=1),
        'lighter_rh': ExchangeOrderModeConfig(order_mode='limit', priority=2, maker_mode=True, post_only=True,
                                              maker_poll_interval_seconds=0.05, **maker),
    }
    config = ExecutionConfig(exchange_order_modes=modes)
    config.order_execution.limit_order_timeout = 1
    e = ArbitrageExecutor(config, {'lighter': main, 'lighter_rh': rh}, monitor_only=False, is_segmented_mode=True,
                          symbol_state_manager=SimpleNamespace(defer=lambda **kwargs: None))
    e.data_processor = SimpleNamespace(get_orderbook=lambda exchange, symbol, max_age_seconds=None: books.get(exchange))
    request = ExecutionRequest(SYMBOL, 'lighter_rh', 'lighter', D('99.99'), D('100.05'), D('0.1'))
    return e, request, rh, main


def test_plan_puts_maker_on_rh_and_taker_on_lighter():
    e, request, _, _ = maker_executor({}, [])
    plan = e.order_strategy_executor.determine_execution_plan(request)
    assert plan['mode'] == 'limit_market'
    assert plan['first_exchange'] == 'lighter_rh' and plan['second_exchange'] == 'lighter'


def test_unfilled_maker_is_post_only_at_best_bid_cancelled_and_never_hedged():
    books = {'lighter_rh': book('99.98', '99.99'), 'lighter': book('100.05', '100.06')}
    e, request, rh, main = maker_executor(books, [order()] * 200 + [order(status=OrderStatus.CANCELED)] * 50)
    result = run(e.execute_arbitrage(request))
    assert not result.success
    kwargs = rh.create_order.call_args.kwargs
    assert kwargs['order_type'] == OrderType.LIMIT and kwargs['price'] == D('99.98')
    assert kwargs['params']['time_in_force'] == 'POST_ONLY'
    assert rh.cancel_order.await_count == 1
    main.create_order.assert_not_awaited()


def test_maker_not_placed_when_hedge_venue_already_adverse():
    books = {'lighter_rh': book('99.98', '99.99'), 'lighter': book('100.00', '100.01')}  # hedge bid fell 5bp
    e, request, rh, main = maker_executor(books, [])
    result = run(e.execute_arbitrage(request))
    assert not result.success
    rh.create_order.assert_not_awaited(); main.create_order.assert_not_awaited()


def test_maker_cancelled_when_hedge_moves_against_while_resting():
    books = {'lighter_rh': book('99.98', '99.99'), 'lighter': book('100.05', '100.06')}
    calls = {'n': 0}

    async def get_order(order_id, symbol):
        calls['n'] += 1
        if calls['n'] == 2:
            books['lighter'] = book('99.90', '99.91')
        return order(status=OrderStatus.CANCELED) if rh.cancel_order.await_count else order()
    e, request, rh, main = maker_executor(books, [])
    rh.get_order = AsyncMock(side_effect=get_order)
    result = run(e.execute_arbitrage(request))
    assert not result.success and rh.cancel_order.await_count == 1
    main.create_order.assert_not_awaited()


def test_filled_maker_triggers_market_hedge_on_lighter_with_widened_bound():
    books = {'lighter_rh': book('99.98', '99.99'), 'lighter': book('100.05', '100.06')}
    e, request, rh, main = maker_executor(books, [order(status=OrderStatus.FILLED, filled='0.1')])
    request._stock_price_bound_sell = D('100.05')
    seen = {}

    async def fake_market(adapter, symbol, quantity, is_buy, *args, **kwargs):
        seen.update(adapter=adapter, quantity=quantity, is_buy=is_buy, bound=request._stock_price_bound_sell)
        return None
    with patch.object(e, '_place_market_order', new=AsyncMock(side_effect=fake_market)):
        run(e.execute_arbitrage(request))
    assert seen['adapter'] is main and seen['is_buy'] is False and D(str(seen['quantity'])) == D('0.1')
    # Hedge (and its failed-leg retries) may sell down to live*(1-hedge_max_slippage_pct).
    assert seen['bound'] == D('100.05') * (1 - D('0.0005'))


# ---------------------------------------------------------------- symbol management / 24x7 markets

def test_crypto_symbols_skip_stock_session_threshold():
    from zoneinfo import ZoneInfo
    from core.services.arbitrage_monitor_v2.utils.stock_signal import cash_threshold
    night = datetime(2026, 10, 17, 22, 0, tzinfo=ZoneInfo('America/New_York'))
    crypto = SimpleNamespace(initial_spread_threshold=0.05, stock_session={'enabled': False, 'offhours_threshold_pct': 0.18})
    stock = SimpleNamespace(initial_spread_threshold=0.12, stock_session={'offhours_threshold_pct': 0.18, 'calendar_valid_years': [2026]})
    assert cash_threshold(crypto, night) == 0.05
    assert cash_threshold(stock, night) == 0.18


def test_rh_profile_has_crypto_pairs_with_common_grid():
    strategy = yaml.safe_load(SEGMENTED.read_text())
    for name, precision in (('BTC-USDC-PERP', 5), ('ETH-USDC-PERP', 4), ('SOL-USDC-PERP', 3), ('HYPE-USDC-PERP', 2)):
        item = strategy['symbol_configs'][name]
        assert item['grid_config']['stock_session'] == {'enabled': False}
        assert item['quantity_config']['quantity_precision'] == precision


def _manage(tmp_path, monkeypatch):
    import importlib.util, shutil
    for name in ('arbitrage_segmented_rh', 'arbitrage_lighter_rh', 'monitor_lighter_rh', 'multi_exchange_arbitrage_rh'):
        shutil.copy(Path('config/arbitrage') / (name + '.yaml'), tmp_path / (name + '.yaml'))
    spec = importlib.util.spec_from_file_location('manage_symbols', 'scripts/manage_symbols.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'PROFILE_DIR', tmp_path)
    monkeypatch.setattr(module, 'fetch_markets', lambda venue: (_ for _ in ()).throw(AssertionError('network')))
    return module


def test_manage_symbols_add_and_remove_keeps_all_profiles_in_sync(tmp_path, monkeypatch):
    module = _manage(tmp_path, monkeypatch)
    module.main(['add', 'XRP', '--precision', '1', '--min-qty', '20', '--price', '2'])
    load = lambda n: yaml.safe_load((tmp_path / n).read_text())
    assert 'XRP-USDC-PERP' in load('arbitrage_segmented_rh.yaml')['symbol_configs']
    assert 'XRP' in load('arbitrage_segmented_rh.yaml')['market_constraints']
    assert 'XRP-USDC-PERP' in load('arbitrage_lighter_rh.yaml')['arbitrage_execution']['quantity_config']
    assert 'XRP-USDC-PERP' in load('monitor_lighter_rh.yaml')['symbols']
    assert 'XRP-USDC-PERP' in load('multi_exchange_arbitrage_rh.yaml')['symbols']
    validate_stock_profiles(tmp_path / 'arbitrage_segmented_rh.yaml', tmp_path / 'arbitrage_lighter_rh.yaml')
    module.main(['remove', 'XRP', 'HYPE'])
    for name in ('XRP-USDC-PERP', 'HYPE-USDC-PERP'):
        assert name not in load('arbitrage_segmented_rh.yaml')['symbol_configs']
        assert name not in load('monitor_lighter_rh.yaml')['symbols']
        assert name not in load('arbitrage_lighter_rh.yaml')['arbitrage_execution']['quantity_config']
    validate_stock_profiles(tmp_path / 'arbitrage_segmented_rh.yaml', tmp_path / 'arbitrage_lighter_rh.yaml')


def test_manage_symbols_rejects_order_below_venue_minimum_without_writing(tmp_path, monkeypatch):
    module = _manage(tmp_path, monkeypatch)
    before = (tmp_path / 'arbitrage_segmented_rh.yaml').read_text()
    with pytest.raises(SystemExit):
        module.main(['add', 'XRP', '--precision', '0', '--min-qty', '500', '--price', '2'])
    assert (tmp_path / 'arbitrage_segmented_rh.yaml').read_text() == before
