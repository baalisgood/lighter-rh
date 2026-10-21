"""Versioned signal-only checkpoints. Invalid caches always fall back to cold state."""
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile
from collections import deque

from .stock_signal import BasisState


class BasisCache:
    VERSION = 1
    MAX_BYTES = 4_000_000

    def __init__(self, path, configs, logger):
        self.path = Path(path).absolute() if path is not None else None
        self.configs = configs
        self.logger = logger
        self.last_write = None

    def _safe_path(self):
        try:
            resolved = self.path.resolve()
        except RuntimeError as exc:
            raise ValueError('Basis cache path cannot be resolved') from exc
        if self.path != resolved:
            raise ValueError('Basis cache path contains a symlink')
        if self.path.exists() and not self.path.is_file():
            raise ValueError('Basis cache is not a regular file')

    def _fingerprint(self, symbol):
        return hashlib.sha256(json.dumps({"symbol": symbol, "basis": self.configs[symbol]}, sort_keys=True, allow_nan=False).encode()).hexdigest()

    def load(self, now):
        if self.path is None:
            return {}
        try:
            self._safe_path()
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd) as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError('Basis cache is not a regular file')
                raw = handle.read(self.MAX_BYTES + 1)
            if len(raw) > self.MAX_BYTES:
                raise ValueError('Oversized basis cache')
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError('Invalid basis cache schema')
            if type(payload['version']) is not int or payload['version'] != self.VERSION or set(payload) != {'version', 'states'} or not isinstance(payload['states'], dict):
                raise ValueError('Incompatible basis cache')
            states = {}
            for symbol, saved in payload['states'].items():
                if not isinstance(saved, dict) or set(saved) != {'symbol', 'config', 'ema', 'started', 'last', 'valid_seconds', 'mature', 'history'}:
                    raise ValueError('Invalid basis state schema')
                cfg = self.configs[symbol]
                if saved['symbol'] != symbol or saved['config'] != self._fingerprint(symbol):
                    raise ValueError('Basis cache configuration mismatch')
                state = new_basis_state(cfg)
                for name in ('ema', 'started', 'last', 'valid_seconds'):
                    value = saved[name]
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                        raise ValueError('Invalid basis timing/value')
                    setattr(state, name, float(value))
                if state.started < 0 or not 0 <= now - state.last <= state.reset_after or not 0 <= state.valid_seconds <= state.last - state.started:
                    raise ValueError('Expired or inconsistent basis timing')
                history = saved['history']
                if not isinstance(history, list) or not 1 <= len(history) <= math.ceil(state.drift_window + state.reset_after) + 2:
                    raise ValueError('Invalid basis history size')
                previous = None
                offline_seconds = 0.0
                for sample in history:
                    if not isinstance(sample, list) or len(sample) != 2 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in sample):
                        raise ValueError('Invalid basis history')
                    stamp, value = sample
                    if not state.started <= stamp <= state.last or (previous is not None and stamp - previous < 1):
                        raise ValueError('Unsorted basis history')
                    if previous is not None:
                        gap = stamp - previous
                        if gap > state.reset_after:
                            raise ValueError('Basis history spans a reset')
                        if gap > state.max_gap:
                            offline_seconds += gap
                    previous = stamp
                # Pruned lifetime history may contain valid time; visible outages cannot.
                if state.valid_seconds > state.last - state.started - offline_seconds:
                    raise ValueError('Basis duration includes an observed outage')
                if history[-1] != [state.last, state.ema]:
                    raise ValueError('Inconsistent basis history end')
                mature = state.valid_seconds >= max(state.warmup, state.drift_window)
                if type(saved['mature']) is not bool or saved['mature'] != mature or (mature and state.last - history[0][0] < state.drift_window):
                    raise ValueError('Inconsistent basis maturity')
                state.history = deque(map(tuple, history))
                state.recovering = True
                states[symbol] = state
            return states
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            self.logger.warning('Basis checkpoint ignored (%s)', type(exc).__name__)
            return {}

    def checkpoint(self, states, now):
        if self.path is None or not math.isfinite(now):
            return
        interval = min((cfg.get('checkpoint_interval_seconds', 60) for cfg in self.configs.values()), default=60)
        if self.last_write is not None and 0 <= now - self.last_write < interval:
            return
        self.last_write = now
        temporary = None
        try:
            self._safe_path()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._safe_path()
            saved = {}
            for symbol, state in states.items():
                if state.last is None:
                    continue
                saved[symbol] = dict(symbol=symbol, config=self._fingerprint(symbol), ema=state.ema,
                                     started=state.started, last=state.last, valid_seconds=state.valid_seconds,
                                     mature=state.valid_seconds >= max(state.warmup, state.drift_window),
                                     history=list(state.history))
            with tempfile.NamedTemporaryFile(mode='w', dir=self.path.parent, prefix='.stock-basis-', delete=False) as handle:
                temporary = handle.name
                json.dump(dict(version=self.VERSION, states=saved), handle, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            self._safe_path()
            os.replace(temporary, self.path)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.logger.warning('Basis checkpoint write failed (%s)', type(exc).__name__)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


def new_basis_state(cfg):
    return BasisState(window=float(cfg.get('ema_seconds', 3600)), warmup=float(cfg.get('warmup_seconds', 3600)),
                      drift_window=float(cfg.get('drift_seconds', 3600)), drift_limit=float(cfg.get('max_drift_pct', .10)),
                      max_gap=float(cfg.get('max_gap_seconds', 3)), recovery_seconds=float(cfg.get('recovery_seconds', 60)),
                      reset_after=float(cfg.get('reset_after_seconds', 600)))
