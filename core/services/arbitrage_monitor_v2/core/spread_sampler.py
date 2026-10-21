"""轻量价差采样器：统一调度模式下按固定窗口把每个交易对的开/平价差落盘为 CSV。

只做内存聚合 + 每个窗口追加写一次小文件，不访问任何交易所接口（不占 REST 额度）。
分析：python scripts/spread_report.py --hours 1
"""
import csv
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

FIELDS = [
    "ts", "pair", "buy", "sell", "n",
    "open_last", "open_max", "open_min", "open_avg",
    "adj_open_max", "close_last", "close_max", "basis",
]


class SpreadSampler:
    def __init__(self, data_dir: Optional[str] = None, interval_seconds: Optional[float] = None,
                 clock=time.time) -> None:
        self.enabled = os.getenv("SPREAD_SAMPLES", "1") != "0"
        self.data_dir = Path(data_dir or os.getenv("SPREAD_SAMPLE_DIR", "data/spread_samples"))
        self.interval = float(interval_seconds or os.getenv("SPREAD_SAMPLE_INTERVAL", "10"))
        self._clock = clock
        self._window_start = clock()
        self._agg: Dict[str, dict] = {}
        self._failed = False

    def record(self, pair_id: str, opening, closing=None,
               adjusted_open: Optional[float] = None) -> None:
        if not self.enabled or self._failed or opening is None:
            return
        try:
            o = float(opening.spread_pct)
            c = float(closing.spread_pct) if closing is not None else None
            a = float(adjusted_open) if adjusted_open is not None else o
            key = f"{pair_id}|{opening.exchange_buy}|{opening.exchange_sell}"
            s = self._agg.get(key)
            if s is None:
                s = self._agg[key] = {"pair": pair_id, "buy": opening.exchange_buy,
                                      "sell": opening.exchange_sell, "n": 0, "sum": 0.0,
                                      "max": o, "min": o, "adj_max": a, "close_last": c,
                                      "close_max": c}
            s["n"] += 1
            s["sum"] += o
            s["last"] = o
            s["max"] = max(s["max"], o)
            s["min"] = min(s["min"], o)
            s["adj_max"] = max(s["adj_max"], a)
            s["basis"] = o - a
            if c is not None:
                s["close_last"] = c
                s["close_max"] = c if s["close_max"] is None else max(s["close_max"], c)
            if self._clock() - self._window_start >= self.interval:
                self.flush()
        except Exception as exc:  # 采样绝不能影响交易主流程
            self._failed = True
            logger.warning("价差采样已停用: %s", exc)

    def flush(self) -> None:
        now = self._clock()
        rows, self._agg, self._window_start = self._agg, {}, now
        if not rows:
            return
        ts = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
        path = self.data_dir / f"spread_samples_{ts[:10].replace('-', '')}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        r6 = lambda v: "" if v is None else f"{v:.5f}"
        with path.open("a", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(FIELDS)
            for s in rows.values():
                w.writerow([ts, s["pair"], s["buy"], s["sell"], s["n"], r6(s["last"]),
                            r6(s["max"]), r6(s["min"]), r6(s["sum"] / s["n"]), r6(s["adj_max"]),
                            r6(s["close_last"]), r6(s["close_max"]), r6(s["basis"])])
