"""Arcus multiplexed feeds. Reconnect re-subscribes for fresh full snapshots."""
import asyncio
import inspect
import json

import websockets

from ..order_state import OrderStateUnknownError
from ..models import OrderStatus


class ArcusWebSocket:
    def __init__(self, adapter):
        self.adapter = adapter
        self._ws_connected = False
        self._socket = None
        self._task = None
        self._stopping = False
        self._subscriptions = {}
        self._callbacks = {}
        self._book_sequences = {}
        self.order_fill_ws_enabled = True
        self._ready = set()

    async def connect(self):
        if self._task:
            return
        self._stopping = False
        self._socket = await websockets.connect(self.adapter.ws_url, ping_interval=20, ping_timeout=20, open_timeout=self.adapter.config.connect_timeout)
        self._ws_connected = True
        for frame in self._subscriptions.values():
            await self._socket.send(json.dumps(frame))
        self._task = asyncio.create_task(self._run())

    async def disconnect(self):
        self._stopping = True
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass
            self._task = None
        if self._socket:
            await self._socket.close()
        self._ws_connected = False
        self._ready.clear()

    async def _run(self):
        while not self._stopping:
            try:
                async for raw in self._socket:
                    await self.handle_message(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception:
                self.adapter.logger.warning('Arcus WebSocket disconnected; waiting for fresh snapshots')
            self._ws_connected = False
            self._ready.clear()
            self._book_sequences.clear()
            if self._stopping or not self.adapter.config.enable_auto_reconnect:
                return
            await asyncio.sleep(self.adapter.config.retry_delay)
            try:
                self._socket = await websockets.connect(self.adapter.ws_url, ping_interval=20, ping_timeout=20)
                self._ws_connected = True
                for frame in self._subscriptions.values():
                    await self._socket.send(json.dumps(frame))
            except Exception:
                continue

    async def _subscribe(self, channel, callback, symbol=None):
        key = (channel, symbol)
        callbacks = self._callbacks.setdefault(key, [])
        if callback not in callbacks:
            callbacks.append(callback)
        if key in self._subscriptions:
            return
        frame = {'type':'subscribe','channel':channel}
        if channel in ('orders','userFills','positions','account'):
            if not self.adapter.address:
                raise ValueError('Arcus wallet address required for account subscriptions')
            frame.update(id=self.adapter.address, accountIndex=self.adapter.account_index)
        elif symbol:
            frame.update(id=symbol, nLevels=20)
        self._subscriptions[key] = frame
        if self._ws_connected:
            await self._socket.send(json.dumps(frame))

    async def subscribe_orderbook(self, symbol, callback):
        await self._subscribe('l2Orderbook', callback, symbol)

    async def subscribe_ticker(self, symbol, callback):
        self._callbacks.setdefault(('ticker',symbol), []).append(callback)
        await self._subscribe('markets', lambda *args: None)

    async def subscribe_orders(self, callback):
        await self._subscribe('orders', callback)
        # Merge userFills into the same order cache even when executor subscribes only orders.
        await self._subscribe('userFills', lambda *args: None)

    async def subscribe_order_fills(self, callback):
        await self._subscribe('userFills', callback)
        await self._subscribe('orders', lambda *args: None)

    async def subscribe_positions(self, callback):
        await self._subscribe('positions', callback)

    async def subscribe_user_data(self, callback):
        await self._subscribe('account', callback)
        await self.subscribe_orders(lambda *args: None)
        await self.subscribe_positions(lambda *args: None)

    def lookup_cached_order(self, order_id):
        return self.adapter._orders.get(self.adapter._clients.get(str(order_id),str(order_id)))

    async def _emit(self, key, *args):
        for callback in self._callbacks.get(key,[]):
            result = callback(*args)
            if inspect.isawaitable(result):
                await result

    def _scoped_row(self, row, message):
        # An envelope authorizes the scope only after BOTH address and index match.
        return {'address': message['id'], 'accountIndex':message['accountIndex'], **row}

    async def handle_message(self, message):
        channel = message.get('channel'); symbol = message.get('id'); data = message.get('contents',{})
        private = channel in ('orders','userFills','positions','account')
        if private and (str(symbol).lower() != self.adapter.address or message.get('accountIndex') != self.adapter.account_index):
            return
        key = (channel,None if private or channel=='markets' else symbol)
        if message.get('type') == 'degraded':
            self._ready.discard(key)
            return
        if not isinstance(data,dict) or message.get('type') not in ('subscribed','channel_data'):
            return
        if message.get('type') == 'subscribed':
            self._ready.add(key)
        elif key not in self._ready:
            return
        if channel == 'l2Orderbook':
            sequence = int(data.get('lastSequenceId',0))
            if sequence <= self._book_sequences.get(symbol,-1):
                return
            self._book_sequences[symbol] = sequence
            await self._emit(key,symbol,self.adapter._parse_book(symbol,data))
        elif channel == 'markets':
            self.adapter._set_markets(data.get('markets',{}))
            for native, market in self.adapter._markets.items():
                await self._emit(('ticker',native),native,self.adapter._parse_ticker(market))
        elif channel == 'orders':
            rows=[]
            for field in ('orders','openOrders','recentClosedOrders'):
                values=data.get(field,[])
                rows.extend(values.values() if isinstance(values,dict) else values)
            if 'orderId' in data:
                rows.append(data)
            for row in rows:
                order=self.adapter._merge_order(self._scoped_row(row,message))
                if order:
                    await self._emit(('orders',None),order)
        elif channel == 'userFills':
            rows=data.get('fills',[data] if 'orderId' in data else [])
            if isinstance(rows,dict): rows=rows.values()
            for row in rows:
                scoped=self._scoped_row(row,message)
                if not self.adapter._scope_matches(scoped): continue
                identity=(row.get('tradeId'),row.get('orderId'))
                if not identity[0] or identity in self.adapter._fill_ids: continue
                self.adapter._fill_ids.add(identity)
                order_id=str(row['orderId']); fills=self.adapter._fills.setdefault(order_id,{})
                fills[identity]=scoped
                from decimal import Decimal
                cached=self.adapter._orders.get(order_id)
                if cached is None:
                    self.adapter._uncertain.add(order_id)
                    try:
                        await self.adapter.get_order(order_id, self.adapter._symbol_for_market(row.get('marketId')))
                    except Exception:
                        pass
                    continue
                if int(row.get('sequenceNumber',0)) <= int(cached.raw_data.get('sequenceNumber',0)):
                    continue
                if 'originalSize' not in row or 'remainingSize' not in row:
                    self.adapter._uncertain.add(order_id)
                    try:
                        await self.adapter.get_order(order_id, self.adapter._symbol_for_market(row.get('marketId')))
                    except Exception:
                        pass
                    continue
                cumulative=Decimal(str(row['originalSize']))-Decimal(str(row['remainingSize']))
                delta=cumulative-cached.filled
                size=Decimal(str(row['size']))
                if delta != size or delta < 0 or (cached.filled and cached.average is None):
                    if cumulative > cached.filled:
                        cached.filled=cumulative
                        cached.remaining=max(Decimal(0),cached.amount-cumulative)
                        cached.average=None
                        cached.status=OrderStatus.PENDING
                        cached.raw_data['_valuation_unknown']=True
                        cached.raw_data['filledSize']=str(cumulative)
                        cached.raw_data['remainingSize']=str(cached.remaining)
                        cached.raw_data.pop('avgFillPrice',None)
                        cached.raw_data.pop('averageFillPrice',None)
                    self.adapter._uncertain.add(order_id)
                    try:
                        await self.adapter.get_order(order_id, self.adapter._symbol_for_market(row.get('marketId')))
                    except Exception:
                        pass
                    continue
                cost=cached.cost+size*Decimal(str(row['price']))
                # Only a delta consistent with the confirmed baseline yields cumulative valuation.
                order=self.adapter._merge_order({**scoped,'filledSize':str(cumulative),
                    'avgFillPrice':str(cost/cumulative) if cumulative else '0'})
                if order:
                    await self._emit(('orders',None),order)
                    await self._emit(('userFills',None),order)
        elif channel == 'positions':
            if data.get('isSnapshot'):
                self.adapter._positions.clear()
            rows=data.get('positions',{})
            for row in (rows.values() if isinstance(rows,dict) else rows):
                scoped=self._scoped_row(row,message)
                if not self.adapter._scope_matches(scoped): continue
                position=self.adapter._parse_position(scoped)
                previous=self.adapter._positions.get(position.symbol)
                if previous and int(row.get('sequenceNumber',0)) < int(previous.raw_data.get('sequenceNumber',0)): continue
                self.adapter._positions[position.symbol]=position
                await self._emit(key,position)
        elif channel == 'account':
            await self._emit(key,data)

    async def unsubscribe(self,symbol=None):
        for key,frame in list(self._subscriptions.items()):
            if symbol is None or key[1]==symbol:
                if self._ws_connected:
                    await self._socket.send(json.dumps({**frame,'type':'unsubscribe'}))
                self._subscriptions.pop(key,None); self._callbacks.pop(key,None); self._ready.discard(key)
