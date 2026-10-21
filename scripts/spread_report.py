#!/usr/bin/env python3
"""汇总 data/spread_samples/*.csv：每个交易对/方向的价差分布与阈值触达率。

用法: python3 scripts/spread_report.py [--hours 1] [--dir data/spread_samples]
列说明（单位均为 %）:
  win     采样窗口数（默认每窗口10秒）
  open均  窗口内开仓价差均值；p50/p90/p99/max 为各窗口“窗口内最大开仓价差”的分位数
  adj_p90 扣除基差(EMA)后的开仓价差 p90（基差启用时决策用的是这个）
  ≥x%     窗口最大开仓价差 ≥ x% 的窗口占比
  往返    开仓价差 + 同时刻平仓价差（≈立即开平一次的成本，越接近0越好）
"""
import argparse
import csv
import glob
from collections import defaultdict
from datetime import datetime, timedelta

LEVELS = [0.02, 0.03, 0.05, 0.08, 0.12]


def q(vals, p):
    if not vals:
        return float("nan")
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(p * (len(vals) - 1) + 0.5))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=1.0)
    ap.add_argument("--dir", default="data/spread_samples")
    a = ap.parse_args()
    since = (datetime.now() - timedelta(hours=a.hours)).strftime("%Y-%m-%d %H:%M:%S")
    g = defaultdict(lambda: defaultdict(list))
    files = sorted(glob.glob(f"{a.dir}/spread_samples_*.csv"))
    for f in files:
        with open(f) as fh:
            for r in csv.DictReader(fh):
                if r["ts"] < since:
                    continue
                k = (r["pair"], f'{r["buy"]}->{r["sell"]}')
                d = g[k]
                f_ = lambda x: float(x) if x not in ("", None) else None
                d["avg"].append(f_(r["open_avg"]))
                d["max"].append(f_(r["open_max"]))
                d["adj"].append(f_(r["adj_open_max"]))
                c, o = f_(r["close_last"]), f_(r["open_last"])
                if c is not None and o is not None:
                    d["rt"].append(o + c)
                d["n"].append(int(r["n"]))
    print(f"files={len(files)} since={since}")
    if not g:
        print("没有采样数据（确认已更新代码并重启，且运行超过 10 秒）")
        return
    hdr = ["pair", "dir", "win", "open均", "p50", "p90", "p99", "max", "adj_p90"] + [f"≥{x}%" for x in LEVELS] + ["往返均"]
    print(" | ".join(hdr))
    for (pair, dr), d in sorted(g.items()):
        mx, n = d["max"], len(d["max"])
        row = [pair, dr, n, f'{sum(d["avg"]) / n:.4f}', f"{q(mx, .5):.4f}", f"{q(mx, .9):.4f}",
               f"{q(mx, .99):.4f}", f"{max(mx):.4f}", f'{q(d["adj"], .9):.4f}']
        row += [f"{100 * sum(v >= x for v in mx) / n:.1f}" for x in LEVELS]
        row.append(f'{sum(d["rt"]) / len(d["rt"]):.4f}' if d["rt"] else "-")
        print(" | ".join(map(str, row)))


if __name__ == "__main__":
    main()
