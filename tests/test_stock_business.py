import asyncio
from datetime import datetime
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch, AsyncMock
import pytest,yaml
from core.adapters.exchanges.models import OrderBookData,OrderBookLevel,OrderSide,OrderStatus,OrderType,OrderData
from core.services.arbitrage_monitor_v2.analysis.spread_calculator import SpreadData
from core.services.arbitrage_monitor_v2.core.unified_orchestrator import UnifiedOrchestrator
from core.services.arbitrage_monitor_v2.core.orchestrator_bootstrap import OrchestratorBootstrap
from core.services.arbitrage_monitor_v2.execution.arbitrage_executor import ExecutionRequest
ROOT=Path.cwd(); SYMBOLS=[x+'-USDC-PERP' for x in ['MU','SNDK','AMD','NVDA','INTC','SPCX']]
run=asyncio.run
class Fake:
 strict_order_state=True
 def __init__(self,ex): self.exchange_id=ex;self.config=NS(exchange_id=ex);self.positions={};self.writes=[];self.orders={};self._uncertain=set();self.fail=False;self.books=None;self.fraction=D(1)
 async def get_balances(self):return [NS(currency='USDC',total=D(10000),free=D(10000),used=D(0),timestamp=datetime.now())]
 async def get_positions(self):return [NS(symbol=s,size=abs(q),side='long' if q>0 else 'short',entry_price=D(100),mark_price=D(100),timestamp=datetime.now()) for s,q in self.positions.items() if q]
 async def get_open_orders(self):return []
 async def get_order(self,identity,symbol=None):return self.orders[identity]
 async def create_order(self,symbol,side,order_type,amount,price=None,params=None):
  from core.adapters.exchanges.order_state import OrderStateUnknownError
  if self.fail:raise OrderStateUnknownError(self.exchange_id)
  params=params or {}; filled=amount*self.fraction
  self.writes.append((symbol,side,filled,dict(params)))
  signed=filled if side==OrderSide.BUY else -filled
  if params.get('reduce_only') or params.get('reduceOnly'):assert self.positions.get(symbol,D(0))*signed<0 and abs(signed)<=abs(self.positions[symbol])
  self.positions[symbol]=self.positions.get(symbol,D(0))+signed
  book=self.books[symbol];px=book.best_ask.price if side==OrderSide.BUY else book.best_bid.price
  identity=str(len(self.writes));order=OrderData(identity,identity,symbol,side,order_type,amount,price,filled,amount-filled,filled*px,px,OrderStatus.FILLED,datetime.now(),datetime.now(),{'currency':'USDC','cost':D(0)},[],{},{})
  self.orders[identity]=order;return order

def make(profile):
 adapters={x:Fake(x) for x in ['lighter','arcus']}
 def init(bootstrap):bootstrap.orchestrator.exchange_adapters.update(adapters)
 with patch.object(OrchestratorBootstrap,'init_exchange_adapters',init):
  o=UnifiedOrchestrator(segmented_config_path=profile,monitor_config_path=ROOT/'config/arbitrage/monitor_lighter_arcus.yaml',execution_config_path=ROOT/'config/arbitrage/arbitrage_lighter_arcus.yaml',multi_exchange_config_path=ROOT/'config/arbitrage/multi_exchange_arbitrage.yaml')
 o.monitor_only_mode=False;o.executor.monitor_only=False;o._bootstrap_positions_block_open=False
 for ex,a in adapters.items():
  a.books=o.data_processor.orderbooks[ex]
  for s in SYMBOLS:a.books[s]=OrderBookData(s,[OrderBookLevel(D(100),D(1000))],[OrderBookLevel(D(100),D(1000))],datetime.now(),received_timestamp=datetime.now())
 return o
@pytest.fixture
def o():
 o=make(ROOT/'config/arbitrage/arbitrage_segmented.yaml')
 from core.services.arbitrage_monitor_v2.utils.stock_signal import BasisState
 import time
 for s in SYMBOLS:
  o.config_manager.get_config(s).grid_config.stock_session={}
  state=BasisState();state.ema=0.;state.ready=state.stable=True;state.last=time.time();o.decision_engine._basis_states[s]=state
  for ex in ('lighter','arcus'):
   o.data_processor.tickers.setdefault(ex,{})[s]=NS(timestamp=datetime.now(),funding_rate=D(0),raw_data={'fundingRate':'0','nextFundingAt':time.time()+3600})
 return o
def edge(o,s,reverse=False,close=False):
 buy,sell=('arcus','lighter') if reverse else ('lighter','arcus')
 for ex,px in [(buy,D(100)),(sell,D(100) if close else D(101))]:
  b=o.data_processor.orderbooks[ex][s];b.bids[0].price=b.asks[0].price=px;b.timestamp=b.received_timestamp=datetime.now()
 return SpreadData(s,buy,sell,D(100),D(100) if close else D(101),D(1000),D(1000),D(0) if close else D(1),0. if close else 1.)
async def opening(o,s,q,e):
 await o._execute_open_with_lock(symbol=s,open_quantity=q,spread_data=e,funding_rate_data=None,is_last_split=False,grid_level=1,slippage_pct=D('.0001'),buy_limit_offset=None,sell_limit_offset=None,position=o.decision_engine.get_position(s),open_key=s)

@pytest.mark.parametrize('s',SYMBOLS)
@pytest.mark.parametrize('reverse',[False,True])
def test_value_orders_both_directions_partial_fifo_flat(o,s,reverse):
 async def flow():
  from core.services.arbitrage_monitor_v2.models import FundingRateData
  e=edge(o,s,reverse)
  f=FundingRateData(e.exchange_buy,e.exchange_sell,0,0,0,0,True)
  ok,q=await o.decision_engine.should_open(s,e,f,skip_persistence=True)
  assert ok and q>0 and q*max(e.price_buy,e.price_sell)<=250
  await opening(o,s,q,e)
  p=o.decision_engine.get_position(s);assert p and p.total_quantity==q
  close=edge(o,s,not reverse,True)
  partial=q/2
  # Use common step for a genuine partial close.
  step=D(1).scaleb(-o.config_manager.get_config(s).quantity_config.quantity_precision)
  partial=(partial//step)*step
  await o._execute_and_record_close(s,partial,close,'synthetic partial',False,1,D('.0001'),None,None,p,None)
  assert p.total_quantity==q-partial
  await o._execute_and_record_close(s,p.total_quantity,close,'synthetic flat',True,1,D('.0001'),None,None,p,None)
  assert p.total_quantity==0
  assert all(not any(a.positions.values()) for a in o.exchange_adapters.values())
 run(flow())
