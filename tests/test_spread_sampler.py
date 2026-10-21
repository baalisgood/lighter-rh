import csv
import subprocess
import sys
from types import SimpleNamespace

from core.services.arbitrage_monitor_v2.core.spread_sampler import SpreadSampler


def _sp(v, buy="lighter", sell="lighter_rh"):
    return SimpleNamespace(spread_pct=v, exchange_buy=buy, exchange_sell=sell)


def test_sampler_aggregates_and_report(tmp_path):
    t = [1000.0]
    s = SpreadSampler(data_dir=str(tmp_path), interval_seconds=10, clock=lambda: t[0])
    s.record("BTC", _sp(0.01), _sp(-0.03), 0.005)
    s.record("BTC", _sp(0.04), _sp(-0.02), 0.03)
    assert not list(tmp_path.glob("*.csv"))
    t[0] += 11
    s.record("BTC", _sp(0.02), _sp(-0.025), 0.01)
    files = list(tmp_path.glob("*.csv"))
    assert len(files) == 1
    rows = list(csv.DictReader(files[0].open()))
    assert rows[0]["n"] == "3"
    assert float(rows[0]["open_max"]) == 0.04
    assert float(rows[0]["open_min"]) == 0.01
    assert float(rows[0]["adj_open_max"]) == 0.03
    out = subprocess.run([sys.executable, "scripts/spread_report.py", "--hours", "1000000",
                          "--dir", str(tmp_path)], capture_output=True, text=True, check=True).stdout
    assert "BTC | lighter->lighter_rh | 1" in out


def test_sampler_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SPREAD_SAMPLES", "0")
    s = SpreadSampler(data_dir=str(tmp_path), interval_seconds=0)
    s.record("BTC", _sp(0.01))
    assert not list(tmp_path.glob("*.csv"))
