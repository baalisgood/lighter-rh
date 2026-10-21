"""Offline contracts for interrupted basis, checkpoint startup and UI thresholds."""
import asyncio
import copy
import json
import logging
import os
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from core.adapters.exchanges.models import OrderBookData, OrderBookLevel
from core.services.arbitrage_monitor_v2.config.symbol_config import SegmentedConfigManager
from core.services.arbitrage_monitor_v2.core.orchestrator_ui_controller import OrchestratorUIController
from core.services.arbitrage_monitor_v2.core.spread_pipeline import SpreadPipeline
from core.services.arbitrage_monitor_v2.decision.unified_decision_engine import UnifiedDecisionEngine
from core.services.arbitrage_monitor_v2.utils.stock_signal import BasisState

SYMBOL='MU-USDC-PERP'
PROFILE=Path('config/arbitrage/arbitrage_segmented.yaml')
ENGINE_TIME='core.services.arbitrage_monitor_v2.decision.unified_decision_engine.time.time'


def engine(path=None):
    return UnifiedDecisionEngine(SegmentedConfigManager(PROFILE), basis_state_path=path)


def books(now):
    def book(price):
        return OrderBookData(SYMBOL,[OrderBookLevel(D(price),D(1))],[OrderBookLevel(D(price),D(1))],datetime.fromtimestamp(now),received_timestamp=datetime.fromtimestamp(now))
    return {'lighter':book(101),'arcus':book(100)}


def mature():
    s=BasisState()
    for t in range(3601):s.observe(t,1)
    return s


def test_cold_and_short_recovery_preserves_hour_drift():
    s=BasisState()
    for t in range(3600):s.observe(t,1)
    assert not s.ready
    s.observe(3600,1);assert s.ready
    ema=s.ema;s.observe(3601,None);s.observe(3650,None)
    assert s.last==3600 and s.ema==ema and not s.ready
    for t in range(3660,3720):s.observe(t,1)
    assert not s.ready
    s.observe(3720,1);assert s.ready and s.stable
    assert s.started==0 and s.history[0][0]<=120
    s.observe(3721,1000);assert not s.stable
    s.observe(3722,None)
    for t in range(3723,3784):s.observe(t,1)
    assert s.ready and not s.stable  # recovery cannot erase one-hour drift evidence


@pytest.mark.parametrize('gap,reset',[(600,False),(601,True)])
def test_reset_uses_last_valid_even_with_repeated_empty(gap,reset):
    s=mature()
    for t in range(3601,3600+gap):s.observe(t,None)
    s.observe(3600+gap,1)
    assert (s.started==3600+gap)==reset
    assert not s.ready
    if not reset:assert s.ema==1 and s.valid_seconds==3600


def test_immature_walltime_and_clock_rollback_do_not_mature():
    s=BasisState();s.observe(0,1)
    for t in range(600,3601,600):s.observe(t,1)
    assert s.valid_seconds==0 and not s.ready
    s.observe(3599,1);assert s.last is None and not s.ready
    s.observe(float('nan'),1);assert s.last is None


def checkpoint(tmp_path,monkeypatch,maturity=True,actual=False):
    clock=[10000.0];monkeypatch.setattr(ENGINE_TIME,lambda:clock[0])
    path=tmp_path/'data'/'state.json';e=engine(path)
    if maturity and not actual:
        state=mature();state.started+=10000;state.last+=10000
        state.history=type(state.history)((t+10000,value) for t,value in state.history)
        e._basis_states[SYMBOL]=state;clock[0]=13600.0
        e.observe_basis(SYMBOL,books(clock[0]),clock[0])
    else:
        for t in range(3601 if maturity else 101):
            clock[0]=10000.0+t;e.observe_basis(SYMBOL,books(clock[0]),clock[0])
    return path,e,clock


def test_actual_engine_write_restart_and_sixty_second_recovery(tmp_path,monkeypatch):
    path,e,clock=checkpoint(tmp_path,monkeypatch,actual=True)
    raw=json.loads(path.read_text());assert raw['states'][SYMBOL]['mature']
    assert 'positions' not in path.read_text() and os.stat(path).st_mode&0o777==0o600
    clock[0]+=10;restored=engine(path);state=restored._basis_states[SYMBOL]
    assert state.ema==1 and not restored.basis_ready(SYMBOL)
    start=clock[0]
    for t in range(60):
        clock[0]=start+t;restored.observe_basis(SYMBOL,books(clock[0]),clock[0])
    assert not restored.basis_ready(SYMBOL)
    clock[0]=start+60;restored.observe_basis(SYMBOL,books(clock[0]),clock[0])
    assert restored.basis_ready(SYMBOL)


def test_immature_cache_retains_only_observed_progress(tmp_path,monkeypatch):
    path,e,clock=checkpoint(tmp_path,monkeypatch,False)
    # Last periodic checkpoint is at60, not the unsaved40seconds.
    clock[0]+=500;restored=engine(path);state=restored._basis_states[SYMBOL]
    assert state.valid_seconds==60 and not state.ready
    start=clock[0]
    for t in range(61):
        clock[0]=start+t;restored.observe_basis(SYMBOL,books(clock[0]),clock[0])
    assert state.valid_seconds==120 and not state.ready


@pytest.mark.parametrize('bad',['json','schema','version','symbol','config','nan','future','expired','unsorted','too_many','maturity','duration','tail'])
def test_cache_invalid_cold_without_crash(tmp_path,monkeypatch,bad):
    path,e,clock=checkpoint(tmp_path,monkeypatch)
    payload=json.loads(path.read_text());s=payload['states'][SYMBOL]
    if bad=='json':path.write_text('{')
    elif bad=='schema':payload=[]
    elif bad=='version':payload['version']=99
    elif bad=='symbol':s['symbol']='OTHER'
    elif bad=='config':s['config']='bad'
    elif bad=='nan':s['ema']=float('nan')
    elif bad=='future':s['last']=clock[0]+1
    elif bad=='expired':clock[0]+=601
    elif bad=='unsorted':s['history'][1][0]=s['history'][0][0]
    elif bad=='too_many':s['history']*=2
    elif bad=='maturity':s['mature']=False
    elif bad=='duration':s['valid_seconds']=4000
    elif bad=='tail':s['history'][-1][1]=99
    if bad!='json':path.write_text(json.dumps(payload))
    restored=engine(path);assert not restored._basis_states and not restored.basis_ready(SYMBOL)


def test_cache_symlink_fifo_and_write_failure(tmp_path,monkeypatch):
    monkeypatch.setattr(ENGINE_TIME,lambda:10000)
    target=tmp_path/'target';target.write_text('preserve')
    link=tmp_path/'link';link.symlink_to(target)
    e=engine(link);e.observe_basis(SYMBOL,books(10000),10000)
    assert target.read_text()=='preserve' and not e.basis_ready(SYMBOL)
    folder=tmp_path/'folder';folder.mkdir();parent=tmp_path/'parent';parent.symlink_to(folder,target_is_directory=True)
    e=engine(parent/'cache');e.observe_basis(SYMBOL,books(10000),10000);assert not (folder/'cache').exists()
    fifo=tmp_path/'fifo';os.mkfifo(fifo);assert not engine(fifo)._basis_states
    e=engine(target/'impossible');e.observe_basis(SYMBOL,books(10000),10000);assert not e.basis_ready(SYMBOL)


def test_missing_or_future_bbo_immediate_unready_and_future_ready_age(tmp_path,monkeypatch):
    path,e,clock=checkpoint(tmp_path,monkeypatch)
    assert e.basis_ready(SYMBOL)
    e.observe_basis(SYMBOL,books(clock[0]+2),clock[0]);assert not e.basis_ready(SYMBOL)
    assert e._basis_states[SYMBOL].last==clock[0]
    clock[0]-=1;assert not e.basis_ready(SYMBOL)


@pytest.mark.parametrize('age,ok',[(61,True),(299,True),(300,True),(301,False)])
def test_funding_300_second_boundary(monkeypatch,age,ok):
    monkeypatch.setattr(ENGINE_TIME,lambda:10000)
    ticker=NS(timestamp=datetime.fromtimestamp(10000-age),funding_rate=D('.008'),raw_data={'fundingRate':'.001'})
    pipeline=SpreadPipeline(NS(data_processor=NS(get_ticker=lambda *a:ticker),config_manager=SegmentedConfigManager(PROFILE)))
    assert (pipeline._get_funding_rate_data(SYMBOL,'lighter','arcus') is not None)==ok


@pytest.mark.parametrize('next_at',[None,float('nan'),float('inf'),9999,'bad'])
def test_optional_funding_expiry_rejects_invalid(monkeypatch,next_at):
    monkeypatch.setattr(ENGINE_TIME,lambda:10000)
    ticker=NS(timestamp=datetime.fromtimestamp(10000),funding_rate=D('.008'),raw_data={'fundingRate':'.001','nextFundingAt':next_at})
    p=SpreadPipeline(NS(data_processor=NS(get_ticker=lambda *a:ticker),config_manager=SegmentedConfigManager(PROFILE)))
    assert p._get_funding_rate_data(SYMBOL,'lighter','arcus') is None


@pytest.mark.parametrize('utc,initial,ratio,expected',[('2026-10-15T14:00:00+00:00',.12,.5,.12),('2026-10-15T21:00:00+00:00',.12,.5,.18),('2026-10-15T14:00:00+00:00',.22,.25,.22)])
def test_ui_real_unified_effective_thresholds_no_decisions(monkeypatch,utc,initial,ratio,expected):
    e=engine();grid=e.config_manager.get_config(SYMBOL).grid_config
    grid.initial_spread_threshold=initial;grid.first_close_ratio=ratio
    from core.services.arbitrage_monitor_v2.utils.stock_signal import cash_threshold
    monkeypatch.setattr('core.services.arbitrage_monitor_v2.decision.unified_decision_engine.cash_threshold',lambda g:cash_threshold(g,datetime.fromisoformat(utc)))
    def forbidden(*args,**kwargs):raise AssertionError('UI called a decision')
    e.should_open=e.should_close=forbidden
    before=copy.deepcopy(e.positions)
    controller=OrchestratorUIController(NS(decision_engine=e,monitor_config=NS(symbols=[SYMBOL],ui_refresh_interval_ms=1000),symbol_spreads={}),logging.getLogger('test'))
    data=controller._collect_grid_ui_data();assert len(data['segments'])==3
    first=data['segments'][0];assert first['open_threshold']==pytest.approx(expected) and first['close_threshold']==pytest.approx(expected*ratio)
    assert data['segments'][1]['close_threshold']==pytest.approx(expected)
    assert e.positions==before and not hasattr(e,'grid_configs')


def test_ui_legacy_ratio_and_fallback():
    for ratio in (None,.6):
        grid=NS(segment_quantities=[1,2],min_profit_pct=.2,grid_step=.1)
        if ratio is not None:grid.first_close_ratio=ratio
        e=NS(grid_configs={SYMBOL:grid},positions={})
        data=OrchestratorUIController(NS(decision_engine=e,monitor_config=NS(symbols=[SYMBOL],ui_refresh_interval_ms=1000),symbol_spreads={}),logging.getLogger('test'))._collect_grid_ui_data()
        assert data['segments'][0]['close_threshold']==pytest.approx(.2*(.4 if ratio is None else ratio))


def test_main_selects_only_stock_enabled_checkpoint():
    from main_unified import _stock_basis_state_path
    assert _stock_basis_state_path(PROFILE).name=='stock_basis_state.json'


def test_cache_cleanup_failure_is_best_effort(tmp_path,monkeypatch):
    e=engine(tmp_path/'cache')
    import core.services.arbitrage_monitor_v2.utils.stock_basis_cache as cache
    def fail(*args,**kwargs):raise PermissionError('simulated failure')
    monkeypatch.setattr(cache.os,'replace',fail);monkeypatch.setattr(cache.os,'unlink',fail)
    e.observe_basis(SYMBOL,books(10000),10000)
    assert not e.basis_ready(SYMBOL)


def test_ui_real_position_quantities_include_multiple_fills_per_grid(monkeypatch):
    from core.services.arbitrage_monitor_v2.analysis.spread_calculator import SpreadData
    e=engine();spread=SpreadData(SYMBOL,'arcus','lighter',D(100),D(100),D(100),D(100),D(0),.3)
    for i in range(6):asyncio.run(e.record_open(SYMBOL,D('2.5'),spread,entry_price_buy=D(100),entry_price_sell=D(100)))
    before=copy.deepcopy(e.positions)
    controller=OrchestratorUIController(NS(decision_engine=e,monitor_config=NS(symbols=[SYMBOL],ui_refresh_interval_ms=1000),symbol_spreads={}),logging.getLogger('test'))
    data=controller._collect_grid_ui_data()
    assert [row['actual_quantity'] for row in data['segments']]==[5,5,5]
    assert e.positions==before


def test_orchestrator_constructor_restores_explicit_checkpoint(tmp_path,monkeypatch):
    from unittest.mock import patch
    from core.services.arbitrage_monitor_v2.core.unified_orchestrator import UnifiedOrchestrator
    from core.services.arbitrage_monitor_v2.core.orchestrator_bootstrap import OrchestratorBootstrap
    path,e,clock=checkpoint(tmp_path,monkeypatch)
    with patch.object(OrchestratorBootstrap,'init_exchange_adapters',lambda bootstrap:None):
        orchestrator=UnifiedOrchestrator(basis_state_path=path)
    assert SYMBOL in orchestrator.decision_engine._basis_states
    assert not orchestrator.decision_engine.basis_ready(SYMBOL)


@pytest.mark.parametrize('lead,accepted',[(.000001,True),(1,True),(1.001,False)])
def test_bbo_original_one_second_clock_tolerance(lead,accepted):
    e=engine();e.observe_basis(SYMBOL,books(10000+lead),10000)
    assert (e._basis_states[SYMBOL].ema is not None)==accepted


def test_sparse_observed_cache_cannot_claim_offline_warmup(tmp_path,monkeypatch):
    clock=[10000.0];monkeypatch.setattr(ENGINE_TIME,lambda:clock[0])
    path=tmp_path/'sparse.json';e=engine(path)
    for now in range(10000,13601,600):
        clock[0]=now;e.observe_basis(SYMBOL,books(now),now)
    assert e._basis_states[SYMBOL].valid_seconds==0
    assert SYMBOL in engine(path)._basis_states  # genuine immature state is valid
    payload=json.loads(path.read_text());saved=payload['states'][SYMBOL]
    saved['valid_seconds']=3600;saved['mature']=True
    path.write_text(json.dumps(payload))
    restored=engine(path);assert not restored._basis_states
    for now in range(13601,13662):
        clock[0]=now;restored.observe_basis(SYMBOL,books(now),now)
    assert not restored.basis_ready(SYMBOL)
    saved['valid_seconds']=0;saved['mature']=False;saved['history'][1][0]+=1
    path.write_text(json.dumps(payload))
    assert not engine(path)._basis_states  # a visible601second gap implies a reset


def test_pruned_long_mature_cache_with_short_gap_restores(tmp_path,monkeypatch):
    clock=[18400.0];monkeypatch.setattr(ENGINE_TIME,lambda:clock[0])
    state=BasisState()
    for now in range(10000,18001):state.observe(now,1)
    state.observe(18400,1)
    assert state.valid_seconds==8000 and state.history[0][0]>state.started
    path=tmp_path/'long.json';e=engine(path);e._basis_states[SYMBOL]=state
    e._basis_cache.checkpoint(e._basis_states,clock[0])
    restored=engine(path);assert restored._basis_states[SYMBOL].valid_seconds==8000
    for now in range(18401,18462):
        clock[0]=now;restored.observe_basis(SYMBOL,books(now),now)
    assert restored.basis_ready(SYMBOL)


@pytest.mark.parametrize('parent,cycle',[(False,False),(False,True),(True,False),(True,True)])
def test_cache_symlink_loops_are_nonfatal_for_read_and_write(tmp_path,parent,cycle):
    from unittest.mock import Mock
    from core.services.arbitrage_monitor_v2.utils.stock_basis_cache import BasisCache
    first=tmp_path/'loop';second=tmp_path/'other'
    first.symlink_to(second if cycle else first,target_is_directory=parent)
    if cycle:second.symlink_to(first,target_is_directory=parent)
    logger=Mock();path=first/'cache.json' if parent else first
    cache=BasisCache(path,{SYMBOL:engine().config_manager.get_config(SYMBOL).grid_config.basis_config},logger)
    assert cache.load(10000)=={}
    cache.checkpoint({SYMBOL:mature()},10000)
    assert logger.warning.call_count==2 and first.is_symlink()


def test_actual_configured_pair_pipeline_checkpoint_restart_and_ui(tmp_path,monkeypatch):
    from unittest.mock import patch,AsyncMock
    from core.services.arbitrage_monitor_v2.core.unified_orchestrator import UnifiedOrchestrator
    from core.services.arbitrage_monitor_v2.core.orchestrator_bootstrap import OrchestratorBootstrap
    from core.services.arbitrage_monitor_v2.analysis.spread_calculator import SpreadData
    clock=[float(int(__import__('time').time()))];monkeypatch.setattr(ENGINE_TIME,lambda:clock[0])
    path=tmp_path/'pair-state.json'
    def create():
        with patch.object(OrchestratorBootstrap,'init_exchange_adapters',lambda bootstrap:None):
            orchestrator=UnifiedOrchestrator(basis_state_path=path)
        orchestrator._check_and_open=AsyncMock();orchestrator._check_and_close=AsyncMock()
        return orchestrator
    o=create();pair=next(p for p in o.multi_exchange_pairs if p.normalized_symbol()==SYMBOL)
    alias=pair.trading_pair_id;assert alias!=SYMBOL
    def feed(orchestrator,now):
        clock[0]=now
        for exchange,book in books(now).items():
            orchestrator.data_processor.orderbooks.setdefault(exchange,{})[SYMBOL]=book
        asyncio.run(SpreadPipeline(orchestrator)._process_single_trading_pair(pair))
    start=clock[0];feed(o,start)
    assert alias in o.decision_engine._basis_states and path.exists()
    assert alias in json.loads(path.read_text())['states']
    state=BasisState()
    for now in range(int(start)-3600,int(start)+1):state.observe(now,1)
    o.decision_engine._basis_states[alias]=state;o.decision_engine._basis_cache.last_write=None
    feed(o,start+1)
    assert json.loads(path.read_text())['states'][alias]['mature']
    clock[0]=start+11;restored=create()
    assert alias in restored.decision_engine._basis_states and SYMBOL not in restored.decision_engine._basis_states
    assert not restored.decision_engine.basis_ready(alias)
    for now in range(int(start)+11,int(start)+71):feed(restored,now)
    assert not restored.decision_engine.basis_ready(alias)
    feed(restored,start+71);assert restored.decision_engine.basis_ready(alias)
    spread=SpreadData(SYMBOL,'arcus','lighter',D(100),D(100),D(100),D(100),D(0),.3)
    for _ in range(6):asyncio.run(restored.decision_engine.record_open(alias,D('2.5'),spread,entry_price_buy=D(100),entry_price_sell=D(100)))
    before=copy.deepcopy(restored.decision_engine.positions)
    restored.monitor_config.symbols=[SYMBOL]
    rows=OrchestratorUIController(restored,logging.getLogger('test'))._collect_grid_ui_data()['segments']
    assert [row['actual_quantity'] for row in rows]==[5,5,5]
    assert restored.decision_engine.positions==before
    payload=json.loads(path.read_text());saved=payload['states'].pop(alias)
    other=next(p.trading_pair_id for p in o.multi_exchange_pairs if p.normalized_symbol()!=SYMBOL)
    saved['symbol']=other;payload['states'][other]=saved;path.write_text(json.dumps(payload))
    assert not create().decision_engine._basis_states  # a different registered pair has a different identity hash
