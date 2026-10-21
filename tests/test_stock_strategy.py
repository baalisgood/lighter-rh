"""Offline decision and configuration behavior; all quotes and fills are synthetic."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
import yaml
from core.services.arbitrage_monitor_v2.config.stock_preflight import validate_stock_profiles
from core.services.arbitrage_monitor_v2.config.symbol_config import SegmentedConfigManager
from core.services.arbitrage_monitor_v2.decision.unified_decision_engine import UnifiedDecisionEngine
from core.services.arbitrage_monitor_v2.analysis.spread_calculator import SpreadData
from core.services.arbitrage_monitor_v2.utils.stock_signal import BasisState, cash_threshold
from core.services.arbitrage_monitor_v2.utils.orderbook_aggregation import aggregate_top_n_vwap
from core.services.arbitrage_monitor_v2.core.spread_pipeline import SpreadPipeline
from core.adapters.exchanges.models import OrderBookData, OrderBookLevel

PROFILE=Path('config/arbitrage/arbitrage_segmented.yaml')
EXEC=Path('config/arbitrage/arbitrage_lighter_arcus.yaml')
SYMBOL='MU-USDC-PERP'
run=asyncio.run

def engine():
    e=UnifiedDecisionEngine(SegmentedConfigManager(PROFILE),deduct_fees=False)
    e.config_manager.symbol_configs[SYMBOL].grid_config.stock_session={}
    return e

def edge(raw=.3,buy='arcus',price=100):
    return SpreadData(SYMBOL,buy,'lighter' if buy=='arcus' else 'arcus',D(price),D(price),D(100),D(100),D(0),raw)

def ready(e,basis):
    now=__import__('time').time()
    state=BasisState();state.ema=basis;state.ready=state.stable=True;state.last=now
    e._basis_states[SYMBOL]=state

def funding(buy=0,sell=0):return NS(funding_rate_buy=buy,funding_rate_sell=sell)

def test_time_ema_one_hz_gap_and_hour_drift():
    s=BasisState()
    s.observe(0,1);s.observe(.5,100);assert s.ema==1
    for t in range(1,3601):s.observe(t,1)
    assert s.ready and s.stable
    s.observe(3601,1000);assert not s.stable
    s.observe(3605,1);assert not s.ready and s.started==0
    s.observe(3606,float('nan'));assert s.ema is not None and not s.ready

@pytest.mark.parametrize('basis,buy,raw,adjusted',[(1,'arcus',1.3,.3),(-1,'arcus',-.7,.3),(1,'lighter',-.7,.3),(-1,'lighter',1.3,.3)])
def test_basis_sign_raw_negative_and_close_direction(basis,buy,raw,adjusted):
    e=engine();ready(e,basis);spread=edge(raw,buy)
    assert e.adjusted_open_spread(SYMBOL,spread)==pytest.approx(adjusted)
    ok,q=run(e.should_open(SYMBOL,spread,funding(),skip_persistence=True));assert ok and q>0
    assert q*D(100)<=250
    run(e.record_open(SYMBOL,q,spread,entry_price_buy=D(100),entry_price_sell=D(100)))
    closing=edge(-raw,'lighter' if buy=='arcus' else 'arcus')
    assert e.close_relative_spread(SYMBOL,closing,-1)==pytest.approx(adjusted)
    assert spread.spread_pct==raw and spread.price_buy==100
    assert set(e.open_direction.values())=={1}

def test_warmup_and_missing_funding_block_only_opens():
    e=engine();spread=edge()
    assert not run(e.should_open(SYMBOL,spread,funding(),skip_persistence=True))[0]
    ready(e,0)
    assert not run(e.should_open(SYMBOL,spread,None,skip_persistence=True))[0]
    assert run(e.should_open(SYMBOL,spread,funding(),skip_persistence=True))[0]
    assert not run(e.should_open(SYMBOL,spread,funding(.3,0),skip_persistence=True))[0]
    e._config(SYMBOL).risk_config.enable_funding_rate_risk=False
    assert run(e.should_open(SYMBOL,spread,None,skip_persistence=True))[0]


def test_entry_notional_keeps_grid_when_market_moves_and_partial_fifo():
    e=engine();ready(e,0)
    spread=edge(.13)
    run(e.record_open(SYMBOL,D(2),spread,entry_price_buy=D(100),entry_price_sell=D(125)))
    run(e.record_open(SYMBOL,D(1),spread,entry_price_buy=D(200),entry_price_sell=D(250)))
    assert e.remaining_entry_notional(SYMBOL)==500 and e.get_current_segments(SYMBOL)==1
    assert e._quantity_after_fifo_close(SYMBOL,D(250))==1  # first lot removed, second retained
    high=edge(.13,price=1000)
    ok,q=run(e.should_open(SYMBOL,high,funding(),skip_persistence=True));assert not ok and q==0
    run(e.record_close(SYMBOL,D(1),edge(0),reason='synthetic partial'))
    assert e.remaining_entry_notional(SYMBOL)==375 and e.get_current_segments(SYMBOL)==1


def test_basis_unavailable_fifo_profitable_close_not_unprofitable():
    e=engine();spread=edge()
    run(e.record_open(SYMBOL,D(1),spread,entry_price_buy=D(100),entry_price_sell=D(101)))
    close=edge(-.01,'lighter');close.price_buy=D(100);close.price_sell=D(100)
    assert run(e.should_close(SYMBOL,close,skip_persistence=True))[0]
    close.price_buy=D(102)
    assert not run(e.should_close(SYMBOL,close,skip_persistence=True))[0]

@pytest.mark.parametrize('utc,expected',[('2026-10-15T13:29:59+00:00',.18),('2026-10-15T13:30:00+00:00',.12),('2026-10-15T20:00:00+00:00',.18),('2026-11-26T16:00:00+00:00',.18),('2026-11-27T17:59:59+00:00',.12),('2026-11-27T18:00:00+00:00',.18),('2028-10-16T15:00:00+00:00',.18)])
def test_cash_calendar_dst_holiday_earlyclose_unknown(utc,expected):
    grid=SegmentedConfigManager(PROFILE).get_config(SYMBOL).grid_config
    assert cash_threshold(grid,datetime.fromisoformat(utc))==expected
    if expected==.12:
        grid.initial_spread_threshold=.22
        assert cash_threshold(grid,datetime.fromisoformat(utc))==.22


def test_protected_aggregate_excludes_outside_price_band():
    b=OrderBookData(SYMBOL,[OrderBookLevel(D(100),D(1))],[OrderBookLevel(D(100),D(1)),OrderBookLevel(D('100.005'),D(1)),OrderBookLevel(D('100.02'),D(50))],datetime.now())
    a=aggregate_top_n_vwap(b,is_buy=True,max_levels=3,target_qty=D(3),price_limit=D('100.01'))
    assert not a.fully_covered and a.total_available==2
    assert aggregate_top_n_vwap(b,is_buy=True,max_levels=3,target_qty=D(2),price_limit=D('100.01')).fully_covered

@pytest.mark.parametrize('mutation',['fees','amount','precision','symbols','mode','nan','negative','ratio','basisoff'])
def test_preflight_rejects_mismatch_and_invalid(tmp_path,mutation):
    a=yaml.safe_load(PROFILE.read_text());b=yaml.safe_load(EXEC.read_text());q=b['arbitrage_execution']['quantity_config'][SYMBOL]
    if mutation=='fees':b['arbitrage_execution']['exchange_fees']['arcus']['market_fee_rate']=.01
    if mutation=='amount':q['single_order_amount_usdc']=300
    if mutation=='precision':q['quantity_precision']=8
    if mutation=='symbols':del b['arbitrage_execution']['quantity_config'][SYMBOL]
    if mutation=='mode':b['arbitrage_execution']['exchange_order_modes']['lighter']['order_mode']='limit'
    if mutation=='nan':a['default_config']['grid_config']['basis_config']['ema_seconds']=float('nan')
    if mutation=='negative':a['symbol_configs'][SYMBOL]['risk_config']['max_position_value']=-1
    if mutation=='ratio':a['default_config']['grid_config']['first_close_ratio']=1.1
    if mutation=='basisoff':a['default_config']['grid_config']['basis_config']['enabled']=False;q['target_value_usdc']=999
    p=tmp_path/'a.yaml';p.write_text(yaml.safe_dump(a));x=tmp_path/'b.yaml';x.write_text(yaml.safe_dump(b))
    with pytest.raises(ValueError):validate_stock_profiles(p,x)


def test_cached_funding_hourly_conversion_missing_stale_not_zero():
    now=datetime.now();cache={}
    o=NS(data_processor=NS(get_ticker=lambda ex,s:cache.get(ex)),config_manager=SegmentedConfigManager(PROFILE))
    pipeline=SpreadPipeline(o)
    cache['lighter']=NS(timestamp=now,funding_rate=D('.008'))
    cache['arcus']=NS(timestamp=now,funding_rate=D('.016'),raw_data={'fundingRate':'.002','nextFundingAt':now.timestamp()+3600})
    f=pipeline._get_funding_rate_data(SYMBOL,'lighter','arcus')
    assert f.funding_rate_buy==pytest.approx(.1) and f.funding_rate_sell==pytest.approx(.2)
    cache['arcus'].raw_data={};assert pipeline._get_funding_rate_data(SYMBOL,'lighter','arcus') is None
    cache['arcus'].raw_data={'fundingRate':'.002'};cache['arcus'].timestamp=now-timedelta(seconds=301)
    assert pipeline._get_funding_rate_data(SYMBOL,'lighter','arcus') is None


def test_continuous_unfavorable_hours_are_observed_per_symbol_not_unknown():
    e=engine();risk=e._config(SYMBOL).risk_config;risk.max_unfavorable_funding_hours=1
    adverse=NS(is_favorable_for_position=False,funding_rate_diff=.02)
    for t in range(0,3600,30):assert e.funding_risk_reason(SYMBOL,adverse,now=t) is None
    assert e.funding_risk_reason(SYMBOL,adverse,now=3600)
    assert e.funding_risk_reason('NVDA-USDC-PERP',adverse,now=3600) is None
    assert e.funding_risk_reason(SYMBOL,None,now=3601) is None
    assert e.funding_risk_reason(SYMBOL,adverse,now=7200) is None
    assert e.funding_risk_reason(SYMBOL,adverse,now=10800) is None  # gap is not adverse observation
    risk.enable_funding_rate_risk=False
    assert e.funding_risk_reason(SYMBOL,adverse,now=10830) is None


def test_equity_gap_alert_fresh_only_and_throttled(monkeypatch):
    from unittest.mock import AsyncMock, patch
    from core.services.arbitrage_monitor_v2.config.arbitrage_config import RiskControlConfig
    from core.services.arbitrage_monitor_v2.risk_control.global_risk_controller import GlobalRiskController
    from core.adapters.exchanges.models import BalanceData
    config=RiskControlConfig();config.balance_management.equity_gap_alert_enabled=True
    config.balance_management.min_balance_warning=0;config.balance_management.min_balance_close_position=0
    def balance(total):return [BalanceData('USDC',D(total),D(0),D(total),D(total),datetime.now(),{})]
    adapters={x:NS(get_balances=AsyncMock(return_value=balance(v)),get_positions=AsyncMock(return_value=[])) for x,v in [('lighter',5000),('arcus',6500)]}
    c=GlobalRiskController(config,adapters)
    with patch('core.services.arbitrage_monitor_v2.risk_control.global_risk_controller.logger.warning') as log:
        run(c._check_all_balances());run(c._check_all_balances())
        assert sum('权益差额' in str(call) for call in log.call_args_list)==1
        c._last_equity_gap_alert=0;adapters['arcus'].get_balances=AsyncMock(return_value=[])
        run(c._check_all_balances())
        assert sum('权益差额' in str(call) for call in log.call_args_list)==1
    assert not c.risk_status.is_paused


def test_session_threshold_switch_restarts_persistence(monkeypatch):
    e=engine();cfg=e._config(SYMBOL)
    now=[1000.0];monkeypatch.setattr('core.services.arbitrage_monitor_v2.decision.unified_decision_engine.time.time',lambda:now[0])
    assert not e._check_spread_persistence('signal',.3,.12,cfg)
    now[0]+=1;assert not e._check_spread_persistence('signal',.3,.12,cfg)
    now[0]+=1;assert not e._check_spread_persistence('signal',.3,.18,cfg)


def test_flat_clears_funding_timer_and_value_ignores_fixed_base():
    e=engine();ready(e,0);e._config(SYMBOL).quantity_config.base_quantity=D(0)
    spread=edge(.13);run(e.record_open(SYMBOL,D(5),spread,entry_price_buy=D(100),entry_price_sell=D(100)))
    assert e.get_current_segments(SYMBOL)==1
    assert e._get_close_persistence_threshold(D(5),e._config(SYMBOL),SYMBOL)==pytest.approx(.06)
    e._unfavorable_funding[SYMBOL]=(0,100)
    run(e.record_close(SYMBOL,D(5),edge(0,'lighter'),reason='flat'))
    assert SYMBOL not in e._unfavorable_funding


@pytest.mark.parametrize('side,bound,expected',[('buy','100.015',10001),('sell','99.995',10000)])
def test_lighter_actual_signed_payload_inward_tick_bound(side,bound,expected):
    from unittest.mock import AsyncMock, Mock
    from core.adapters.exchanges.adapters.lighter_rest import LighterRest
    r=object.__new__(LighterRest)
    r.strict_order_state=True
    r.signer_client=NS(sign_create_order=Mock(return_value=(14,'synthetic','hash',None)))
    r._get_market_info=AsyncMock(return_value={'price_decimals':2,'price_multiplier':100,'market_index':1})
    r._calculate_slippage_protection_price=AsyncMock(return_value=D(101) if side=='buy' else D(99))
    r._next_order_nonce=AsyncMock(return_value=(2,11))
    result=run(r._build_signed_market_order_tx(SYMBOL,side,D(1),client_order_id=42,target_price=D(100),price_bound=D(bound),reference_price=D(100),max_order_notional_usdc=D(250)))
    assert result and r.signer_client.sign_create_order.call_args.kwargs['price']==expected
    assert r.signer_client.sign_create_order.call_args.kwargs['base_amount']==10000


def test_actual_bbo_stale_invalid_interrupts_basis_warmup():
    e=engine();now=float(int(__import__('time').time()))
    def book(px,stamp):return OrderBookData(SYMBOL,[OrderBookLevel(D(px),D(1))],[OrderBookLevel(D(px),D(1))],datetime.fromtimestamp(stamp),received_timestamp=datetime.fromtimestamp(stamp))
    e.observe_basis(SYMBOL,{'lighter':book(101,now),'arcus':book(100,now)},now=now)
    assert e._basis_states[SYMBOL].ema==pytest.approx(1)
    e.observe_basis(SYMBOL,{'lighter':book(101,now-10),'arcus':book(100,now)},now=now+1)
    assert e._basis_states[SYMBOL].ema==pytest.approx(1) and not e._basis_states[SYMBOL].ready
    e.observe_basis(SYMBOL,{'lighter':book(101,now+2),'arcus':book(100,now+2)},now=now+2)
    assert e._basis_states[SYMBOL].started==now and not e._basis_states[SYMBOL].ready
