"""Regression cases derived from independent review and acceptance failures."""
import asyncio
import time
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import pytest
import yaml
import test_stock_business as native
from core.adapters.exchanges.models import OrderBookLevel as L
from core.services.arbitrage_monitor_v2.analysis.spread_calculator import SpreadData
from core.services.arbitrage_monitor_v2.execution.arbitrage_executor import ExecutionRequest, ArbitrageExecutor
from core.services.arbitrage_monitor_v2.config.stock_preflight import validate_stock_profiles
from core.services.arbitrage_monitor_v2.utils.stock_signal import BasisState

S='MU-USDC-PERP'
PROFILE=Path('config/arbitrage/arbitrage_segmented.yaml')
EXEC=Path('config/arbitrage/arbitrage_lighter_arcus.yaml')
run=asyncio.run


def make_orchestrator(symbol=S):
    o=native.make(PROFILE)
    o.config_manager.get_config(symbol).grid_config.stock_session={}
    state=BasisState();state.ema=0;state.ready=state.stable=True;state.last=time.time()
    o.decision_engine._basis_states[symbol]=state
    refresh(o,symbol,time.time())
    return o


def refresh(o,symbol,now):
    for exchange in ('lighter','arcus'):
        book=o.data_processor.orderbooks[exchange][symbol]
        book.timestamp=book.received_timestamp=datetime.fromtimestamp(now)
        o.data_processor.tickers.setdefault(exchange,{})[symbol]=NS(timestamp=datetime.fromtimestamp(now),funding_rate=D(0),raw_data={'fundingRate':'0','nextFundingAt':now+3600})


def thin_edge(o,thin=True):
    for exchange,px in [('lighter',D(100)),('arcus',D('100.17'))]:
        b=o.data_processor.orderbooks[exchange][S]
        b.bids=[L(px,D(10))];b.asks=[L(px,D(10))]
    if thin:
        o.data_processor.orderbooks['lighter'][S].asks=[L(D(100),D('.01')),L(D('100.01'),D(10))]
        o.data_processor.orderbooks['arcus'][S].bids=[L(D('100.17'),D('.01')),L(D('100.16'),D(10))]
    return SpreadData(S,'lighter','arcus',D(100),D('100.17'),D(10),D(10),D('.17'),.17)


def test_slow_accounts_recheck_exact_protected_vwap():
    o=make_orchestrator();edge=thin_edge(o,False)
    request=ExecutionRequest(S,'lighter','arcus',D(100),D('100.17'),D(2),spread_data=edge,grid_level=1,grid_threshold_pct=D('.12'),slippage_tolerance_pct=D('.0001'))
    request._expected_position_quantity=D(0)
    old=o.exchange_adapters['arcus'].get_open_orders
    async def after_read():
        thin_edge(o,True)
        return await old()
    o.exchange_adapters['arcus'].get_open_orders=after_read
    allowed,reason=run(o._validate_segmented_open(request))
    assert not allowed and '价差' in reason


def test_open_persistence_starts_only_when_quantity_vwap_passes():
    o=make_orchestrator();clock=[time.time()];captured=[]
    async def capture(**kwargs):captured.append(kwargs)
    o._execute_open_with_lock=capture
    async def flow():
        for i in range(5):
            clock[0]+=1;refresh(o,S,clock[0]);edge=thin_edge(o,i<2)
            # The seeded mature basis remains freshly observed while the quote-depth signal changes.
            o.decision_engine._basis_states[S].last=clock[0]
            o._record_price_sample(S,edge)
            funding=o._get_funding_rate_data(S,'lighter','arcus')
            await o._check_and_open(S,edge,funding)
            pending=[t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if pending:await asyncio.gather(*pending)
            assert len(captured)==(1 if i==4 else 0)
    with patch('time.time',lambda:clock[0]):run(flow())


def test_close_threshold_uses_protected_vwap_before_scheduling():
    o=make_orchestrator();entry=SpreadData(S,'lighter','arcus',D(100),D(101),D(10),D(10),D(1),1.)
    run(o.decision_engine.record_open(S,D(2),entry,entry_price_buy=D(100),entry_price_sell=D(101)))
    for ex,px in [('lighter',D('99.95')),('arcus',D(100))]:
        b=o.data_processor.orderbooks[ex][S]
        b.bids=[L(px,D('.01')),L(px-D('.009'),D(10))]
        b.asks=[L(px,D('.01')),L(px+D('.009'),D(10))]
        o.exchange_adapters[ex].positions[S]=D(2) if ex=='lighter' else D(-2)
    close=SpreadData(S,'arcus','lighter',D(100),D('99.95'),D(10),D(10),D('-.05'),-.05)
    captured=[]
    async def capture(**kwargs):captured.append(kwargs)
    original=o.decision_engine.should_close
    async def no_wait(*args,**kwargs):return await original(*args,**kwargs,skip_persistence=True)
    o.decision_engine.should_close=no_wait;o._passes_price_stability=lambda *a,**k:True;o._execute_close_with_lock=capture
    async def flow():await o._check_and_close(S,close,None);await asyncio.sleep(0)
    run(flow());assert captured==[]

@pytest.mark.parametrize('kind',['horizon','session','missing_constraint','missing_venue','market_only'])
def test_effective_symbol_preflight_fails_closed(tmp_path,kind):
    a=yaml.safe_load(PROFILE.read_text());b=yaml.safe_load(EXEC.read_text())
    if kind=='horizon':a['symbol_configs'][S]['grid_config']['basis_config']={'funding_horizon_hours':-1}
    if kind=='session':a['symbol_configs'][S]['grid_config']['stock_session']={'offhours_threshold_pct':-1}
    if kind=='missing_constraint':del a['market_constraints']['MU']
    if kind=='missing_venue':del a['market_constraints']['MU']['min_notional_by_exchange']['lighter']
    if kind=='market_only':
        a['decision']['exchange_order_modes']['lighter']['order_mode']='limit'
        b['arbitrage_execution']['exchange_order_modes']['lighter']['order_mode']='limit'
    pa=tmp_path/'a.yaml';pb=tmp_path/'b.yaml';pa.write_text(yaml.safe_dump(a));pb.write_text(yaml.safe_dump(b))
    with pytest.raises(ValueError):validate_stock_profiles(pa,pb)


def test_executor_does_not_ignore_missing_active_constraint():
    executor=object.__new__(ArbitrageExecutor);executor.market_constraints={'OTHER':{}}
    req=ExecutionRequest(S,'lighter','arcus',D(100),D(100),D('.00001'))
    with pytest.raises(ValueError):executor._validate_pair_quantity(req)


def test_close_split_leaves_tradeable_tail():
    o=make_orchestrator();e=o.decision_engine;entry=native.edge(o,S)
    run(e.record_open(S,D('2.5001'),entry,entry_price_buy=D(100),entry_price_sell=D(101)))
    e._basis_states[S].reset();close=native.edge(o,S,True,True)
    ok,q,_,_=run(e.should_close(S,close,skip_persistence=True))
    assert ok and D(0)<q<D('2.5')
    run(e.record_close(S,q,close,reason='partial'))
    left=e.get_position(S).total_quantity
    assert left>=e.minimum_close_quantity(S,close)
    assert run(e.should_close(S,close,skip_persistence=True))[0]

@pytest.mark.parametrize('symbol',native.SYMBOLS)
def test_existing_step_dust_is_explicitly_blocked_without_orders(symbol):
    o=make_orchestrator(symbol);e=o.decision_engine;spread=native.edge(o,symbol)
    q=D(1).scaleb(-o.config_manager.get_config(symbol).quantity_config.quantity_precision)
    run(e.record_open(symbol,q,spread,entry_price_buy=D(100),entry_price_sell=D(101)))
    for ex,adapter in o.exchange_adapters.items():adapter.positions[symbol]=q*(1 if ex=='lighter' else -1)
    p=e.get_position(symbol);close=native.edge(o,symbol,True,True)
    run(o._execute_and_record_close(symbol,q,close,'known remainder',True,1,D('.0001'),None,None,p,None))
    assert p.total_quantity==q and 'Manual review' in e.close_blocked_reasons[symbol]
    assert all(not adapter.writes for adapter in o.exchange_adapters.values())
    assert not run(e.should_open(symbol,spread,None,skip_persistence=True))[0]


def test_legacy_fixed_aggregation_keeps_all_levels(monkeypatch):
    o=make_orchestrator();cfg=o.config_manager.get_config(S)
    cfg.quantity_config.quantity_mode='fixed';cfg.grid_config.basis_config={};cfg.grid_config.stock_session={}
    o.decision_engine.should_open=AsyncMock(return_value=(True,D(2)))
    o._passes_price_stability=lambda *a,**k:True
    from core.services.arbitrage_monitor_v2.utils import orderbook_aggregation as module
    old=module.aggregate_top_n_vwap;limits=[]
    def aggregate(*args,**kwargs):limits.append(kwargs.get('price_limit'));return old(*args,**kwargs)
    monkeypatch.setattr(module,'aggregate_top_n_vwap',aggregate)
    edge=thin_edge(o,False)
    o.data_processor.orderbooks['lighter'][S].asks=[L(D(100),D('.01')),L(D('100.5'),D(10))]
    o.data_processor.orderbooks['arcus'][S].bids=[L(D('100.17'),D('.01')),L(D('99.5'),D(10))]
    o._execute_open_with_lock=AsyncMock()
    async def flow():await o._check_and_open(S,edge,None);await asyncio.sleep(0)
    run(flow());assert limits and all(limit is None for limit in limits)


def test_first_open_check_detects_recovered_dust_without_close_call():
    o=make_orchestrator();e=o.decision_engine;edge=native.edge(o,S)
    run(e.record_open(S,D('.0001'),edge,entry_price_buy=D(100),entry_price_sell=D(101)))
    assert not getattr(e, 'close_blocked_reasons', {})
    assert not run(e.should_open(S,edge,None,skip_persistence=True))[0]
    assert 'Manual review' in e.close_blocked_reasons[S]
    assert e.get_position(S).total_quantity==D('.0001')


@pytest.mark.parametrize('quantity',['5.0001','5.01'])
def test_small_grid_trim_does_not_lock_tradeable_position(quantity):
    o=make_orchestrator();e=o.decision_engine;entry=native.edge(o,S)
    run(e.record_open(S,D(quantity),entry,entry_price_buy=D(100),entry_price_sell=D(100)))
    close=SpreadData(S,'arcus','lighter',D(100),D('99.9'),D(10),D(10),D('-.1'),-.1)
    ok,q,_,_=run(e.should_close(S,close,skip_persistence=True))
    assert not ok and q==0
    assert S not in getattr(e,'close_blocked_reasons',{})
    assert e.get_position(S).total_quantity==D(quantity)
    larger=native.edge(o,S)
    funding=o._get_funding_rate_data(S,larger.exchange_buy,larger.exchange_sell)
    allowed,new_qty=run(e.should_open(S,larger,funding,skip_persistence=True))
    assert allowed and new_qty>0


@pytest.mark.parametrize('risk_exit',[False,True])
def test_slow_close_accounts_recheck_threshold_inside_original_price_band(risk_exit):
    o=make_orchestrator();e=o.decision_engine;entry=native.edge(o,S)
    run(e.record_open(S,D(2),entry,entry_price_buy=D(100),entry_price_sell=D(101)))
    for ex,px in [('lighter',D('99.95')),('arcus',D(100))]:
        b=o.data_processor.orderbooks[ex][S]
        b.bids=[L(px,D(10))];b.asks=[L(px,D(10))]
        o.exchange_adapters[ex].positions[S]=D(2) if ex=='lighter' else D(-2)
    close=SpreadData(S,'arcus','lighter',D(100),D('99.95'),D(10),D(10),D('-.05'),-.05)
    req=ExecutionRequest(S,'arcus','lighter',D(100),D('99.95'),D(2),is_open=False,spread_data=close,grid_action='close',grid_level=1,grid_threshold_pct=D('.06'),slippage_tolerance_pct=D('.0001'),allow_when_global_paused=risk_exit)
    req._stock_price_bound_buy=D('100.01');req._stock_price_bound_sell=D('99.940005')
    original=o.exchange_adapters['lighter'].get_positions
    async def late_change():
        for ex,px in [('arcus',D('100.009')),('lighter',D('99.941'))]:
            b=o.data_processor.orderbooks[ex][S];b.asks[0].price=b.bids[0].price=px
        return await original()
    o.exchange_adapters['lighter'].get_positions=late_change
    allowed,reason=run(o._validate_segmented_close(req))
    assert allowed is risk_exit
    if not risk_exit:assert '平仓阈值' in reason
    assert D('100.009')<req._stock_price_bound_buy and D('99.941')>req._stock_price_bound_sell
