"""Lighter 本地订单簿只输出前 N 档，避免大盘口（BTC/ETH）每条增量全量排序占满 CPU。"""
import random
import time
from decimal import Decimal

from core.adapters.exchanges.adapters.lighter_websocket import LighterWebSocket


def _ws():
    ws = object.__new__(LighterWebSocket)
    ws._local_orderbooks = {}
    return ws


def _snapshot(n, mid=100000.0, tick=0.1):
    bids = [{"price": f"{mid - (i + 1) * tick:.1f}", "size": "1.5"} for i in range(n)]
    asks = [{"price": f"{mid + (i + 1) * tick:.1f}", "size": "2.5"} for i in range(n)]
    random.shuffle(bids)
    random.shuffle(asks)
    return {"bids": bids, "asks": asks, "timestamp": 1700000000000, "offset": 1}


def test_build_returns_sorted_top_levels_only():
    ws = _ws()
    ws._initialize_orderbook(1, _snapshot(3000))
    book = ws._build_orderbook_from_local("BTC", 1)
    assert len(book.bids) == LighterWebSocket.ORDERBOOK_MAX_LEVELS
    assert len(book.asks) == LighterWebSocket.ORDERBOOK_MAX_LEVELS
    assert book.bids[0].price == Decimal("99999.9")
    assert book.asks[0].price == Decimal("100000.1")
    assert [l.price for l in book.bids] == sorted((l.price for l in book.bids), reverse=True)
    assert [l.price for l in book.asks] == sorted(l.price for l in book.asks)


def test_incremental_updates_keep_best_levels_correct():
    ws = _ws()
    ws._initialize_orderbook(1, _snapshot(500))
    # 删除最优买一、新增更优卖一、调整数量
    ws._apply_orderbook_update(1, {
        "bids": [{"price": "99999.9", "size": "0"}, {"price": "99999.8", "size": "7"}],
        "asks": [{"price": "100000.05", "size": "3"}],
    })
    book = ws._build_orderbook_from_local("BTC", 1)
    assert book.bids[0].price == Decimal("99999.8")
    assert book.bids[0].size == Decimal("7")
    assert book.asks[0].price == Decimal("100000.05")
    assert "99999.9" not in ws._local_orderbooks[1]["bid_px"]


def test_small_book_returns_all_levels():
    ws = _ws()
    ws._initialize_orderbook(2, _snapshot(3))
    book = ws._build_orderbook_from_local("AAPL", 2)
    assert [str(l.price) for l in book.bids] == ["99999.9", "99999.8", "99999.7"]
    assert len(book.asks) == 3


def test_large_book_updates_are_fast():
    ws = _ws()
    ws._initialize_orderbook(1, _snapshot(5000))
    start = time.perf_counter()
    for i in range(500):
        ws._apply_orderbook_update(1, {
            "bids": [{"price": f"{99000 + (i % 900) * 0.1:.1f}", "size": str(1 + i % 3)}],
            "asks": [{"price": f"{101000 - (i % 900) * 0.1:.1f}", "size": str(1 + i % 3)}],
        })
        assert ws._build_orderbook_from_local("BTC", 1) is not None
    elapsed = time.perf_counter() - start
    # 旧实现（全量排序+全量 Decimal）在 5000 档下约数十毫秒/次；新实现应远低于此
    assert elapsed / 500 < 0.01, elapsed
