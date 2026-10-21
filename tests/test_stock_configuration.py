"""Offline native configuration checks; no credentials, network or order submission."""
from decimal import Decimal
from pathlib import Path
import yaml
import pytest
from core.services.arbitrage_monitor_v2.config.symbol_config import SegmentedConfigManager
from core.services.arbitrage_monitor_v2.config.monitor_config import ConfigManager
from core.services.arbitrage_monitor_v2.config.multi_exchange_config import MultiExchangeArbitrageConfigManager

SYMBOLS = {s + '-USDC-PERP' for s in ('MU', 'SNDK', 'AMD', 'NVDA', 'INTC', 'SPCX')}

def test_native_loader_uses_only_six_stocks():
    segmented = SegmentedConfigManager(Path('config/arbitrage/arbitrage_segmented.yaml'))
    monitor = ConfigManager(Path('config/arbitrage/monitor_lighter_arcus.yaml'))
    assert set(monitor.config.symbols) == SYMBOLS
    document = yaml.safe_load(Path('config/arbitrage/arbitrage_segmented.yaml').read_text())
    for symbol in SYMBOLS:
        config = segmented.get_config(symbol)
        specific = document['symbol_configs'][symbol]
        assert config.quantity_config.quantity_mode == 'value'
        assert config.quantity_config.target_value_usdc == Decimal('500')
        assert config.quantity_config.split_order_amount_usdc == Decimal('250')
        assert config.grid_config.max_segments == specific['grid_config']['max_segments']
        assert config.grid_config.initial_spread_threshold == specific['grid_config']['initial_spread_threshold']
        assert config.grid_config.grid_step == specific['grid_config']['grid_step']
        assert config.grid_config.slippage_tolerance == .0001
        assert config.risk_config.max_position_value == specific['risk_config']['max_position_value']

def test_budget_is_configurable_without_stock_runtime(tmp_path):
    data = yaml.safe_load(Path('config/arbitrage/arbitrage_segmented.yaml').read_text())
    data['risk_control']['open_only_close']['max_exchange_position_value']['max_value_usdc'] = 6000
    data['symbol_configs']['MU-USDC-PERP']['risk_config']['max_position_value'] = 1200
    target = tmp_path / 'profile.yaml'
    target.write_text(yaml.safe_dump(data))
    loaded = SegmentedConfigManager(target)
    assert loaded.get_config('MU-USDC-PERP').risk_config.max_position_value == 1200
    assert not Path('stock_runtime.py').exists()
    assert not Path('stock_profile.py').exists()

def test_empty_template_supports_original_auth_loader_and_adapter(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from core.utils.config_loader import ExchangeConfigLoader
    from core.services.arbitrage_monitor_v2.core.orchestrator_bootstrap import OrchestratorBootstrap
    from core.adapters.exchanges.adapters.lighter import LighterAdapter
    example = yaml.safe_load(Path('examples/lighter_config.yaml.example').read_text())
    auth = example['api_config']['auth']
    assert all(auth[k] == '' for k in ('api_key_private_key', 'account_index', 'api_key_index'))
    auth.update(api_key_private_key='synthetic-test-only', account_index=7, api_key_index=2)
    path = tmp_path / 'config/exchanges/lighter_config.yaml'
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(example))
    monkeypatch.chdir(tmp_path)
    for key in list(__import__('os').environ):
        if key.startswith('LIGHTER_'): monkeypatch.delenv(key)
    loader = ExchangeConfigLoader()
    bootstrap = object.__new__(OrchestratorBootstrap)
    config = bootstrap._load_exchange_config('lighter', path, loader)
    stub = SimpleNamespace(logger=None, _load_lighter_config=lambda: example)
    converted = LighterAdapter._convert_config_to_dict(stub, config)
    assert converted['api_key_private_key'] == 'synthetic-test-only'
    assert converted['account_index'] == 7
    assert converted['api_key_index'] == 2
    assert converted['auth_enabled'] is True
    assert converted['strict_order_state'] is True

@pytest.mark.parametrize("mode", ["monitor", "live"])
def test_launcher_lock_and_sigterm_share_engine_pid(tmp_path, mode):
    import os
    import shutil
    import subprocess
    import sys
    import time
    shutil.copy('start.sh', tmp_path / 'start.sh')
    module = Path('core/services/arbitrage_monitor_v2/config/stock_preflight.py')
    (tmp_path / module).parent.mkdir(parents=True)
    shutil.copy(module, tmp_path / module)
    shutil.copytree('config/arbitrage', tmp_path / 'config/arbitrage')
    (tmp_path / '.env').write_text('LIGHTER_API_KEY_PRIVATE_KEY=synthetic-project-only\n')
    (tmp_path / 'main_unified.py').write_text(
        'import os,time,sys,yaml\nfrom pathlib import Path\n'
        f'assert yaml.safe_load(Path(sys.argv[2]).read_text())["system_mode"]["monitor_only"] is {mode != "live"}\n'
        'assert "LIGHTER_API_KEY_PRIVATE_KEY" not in os.environ\n'
        'from dotenv import load_dotenv\n'
        'load_dotenv(Path(".env"), override=False, interpolate=False)\n'
        'assert os.environ["LIGHTER_API_KEY_PRIVATE_KEY"] == "synthetic-project-only"\n'
        'Path("pid.txt").write_text(str(os.getpid()))\ntime.sleep(20)\n')
    env = {'PATH': '/usr/bin:/bin', 'STOCK_PYTHON': sys.executable,
           'LIGHTER_API_KEY_PRIVATE_KEY': 'synthetic-not-inherited'}
    def launch():
        return subprocess.Popen(['bash', 'start.sh', mode], cwd=tmp_path,
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    first = launch()
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / 'pid.txt').exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert int((tmp_path / 'pid.txt').read_text()) == first.pid
        second = launch()
        _, err = second.communicate(timeout=5)
        assert second.returncode != 0 and b'already running' in err
        first.terminate()
        first.wait(timeout=5)
        with __import__('pytest').raises(ProcessLookupError): os.kill(first.pid, 0)
        (tmp_path / 'pid.txt').unlink()
        third = launch()
        try:
            deadline = time.monotonic() + 5
            while not (tmp_path / 'pid.txt').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            assert int((tmp_path / 'pid.txt').read_text()) == third.pid
        finally:
            third.terminate(); third.wait(timeout=5)
    finally:
        if first.poll() is None: first.kill(); first.wait(timeout=5)
    lock = tmp_path / 'run/stock.lock'
    lock.unlink()
    sentinel = tmp_path / 'sentinel.txt'
    sentinel.write_text('unchanged')
    lock.symlink_to(sentinel)
    blocked = launch()
    _, error = blocked.communicate(timeout=5)
    assert blocked.returncode != 0 and b'symlink' in error
    assert sentinel.read_text() == 'unchanged'

def test_all_active_lists_and_shared_budget_are_six_stock_only():
    def load(name):
        return yaml.safe_load(Path('config/arbitrage/' + name + '.yaml').read_text())
    profile = load('arbitrage_segmented')
    assert set(profile['symbol_configs']) == SYMBOLS
    assert set(profile['market_constraints']) == {s.split('-')[0] for s in SYMBOLS}
    assert set(profile['risk_control']['open_only_close']['price_volatility']['trigger_symbols']) == {s.split('-')[0] for s in SYMBOLS}
    assert set(load('monitor_lighter_arcus')['symbols']) == SYMBOLS
    assert set(load('multi_exchange_arbitrage')['symbols']) == SYMBOLS
    assert set(load('segment_symbol_filters')['enabled_symbols']) == SYMBOLS
    assert set(load('arbitrage_lighter_arcus')['arbitrage_execution']['quantity_config']) == SYMBOLS | {'default'}
    assert load('multi_leg_pairs')['pairs'] == []
    assert load('extra_symbols') == {'extra_symbols': [], 'by_exchange': {}}
    manager = MultiExchangeArbitrageConfigManager(Path('config/arbitrage/multi_exchange_arbitrage.yaml'))
    assert set(manager.get_required_symbols()) == SYMBOLS
    assert len(manager.get_pairs()) == 6
    assert all(pair.allow_reverse and {pair.exchange_a, pair.exchange_b} == {'lighter', 'arcus'} for pair in manager.get_pairs())
    guard = profile['risk_control']['open_only_close']
    assert guard['max_exchange_position_value']['max_value_usdc'] > 0
    assert guard['dynamic_max_exchange_position_value']['equity_multiplier'] == 1.0


def test_spcx_native_mapping_quantities_and_exchange_boundaries():
    import pytest
    from types import SimpleNamespace
    from core.services.arbitrage_monitor.utils.symbol_converter import SimpleSymbolConverter
    from core.services.arbitrage_monitor_v2.execution.arbitrage_executor import ArbitrageExecutor, ExecutionRequest
    symbol = 'SPCX-USDC-PERP'
    converter = SimpleSymbolConverter()
    for exchange, native in [('lighter', 'SPCX'), ('arcus', 'SPCX-USD')]:
        assert converter.convert_to_exchange(symbol, exchange) == native
        assert converter.convert_from_exchange(native, exchange) == symbol
    cfg = SegmentedConfigManager(Path('config/arbitrage/arbitrage_segmented.yaml')).get_config(symbol)
    assert cfg.quantity_config.base_quantity == Decimal('3.3632')
    assert cfg.quantity_config.split_order_amount_usdc == Decimal(250)
    assert cfg.quantity_config.quantity_precision == 4
    assert cfg.quantity_config.exchange_min_order_qty == {'default': Decimal('.035'), 'lighter': Decimal('.035'), 'arcus': Decimal('.01')}
    profile = yaml.safe_load(Path('config/arbitrage/arbitrage_segmented.yaml').read_text())
    executor = SimpleNamespace(market_constraints=profile['market_constraints'])
    execution = yaml.safe_load(Path('config/arbitrage/arbitrage_lighter_arcus.yaml').read_text())
    assert execution['arbitrage_execution']['quantity_config'][symbol] == {'quantity_mode': 'value', 'single_order_amount_usdc': 250, 'target_value_usdc': 500, 'max_position_usdc': 1500, 'quantity_precision': 4}
    for buy, sell in [('lighter', 'arcus'), ('arcus', 'lighter')]:
        for quantity in ['1.6816', '3.3632', '6.7264']:
            request = ExecutionRequest(symbol, buy, sell, Decimal('148.66'), Decimal('148.66'), Decimal(quantity))
            ArbitrageExecutor._validate_pair_quantity(executor, request)
        for quantity in ['0.0349', '1.68161']:
            request.quantity = Decimal(quantity)
            with pytest.raises(ValueError): ArbitrageExecutor._validate_pair_quantity(executor, request)
        request.quantity = Decimal('1.6816')
        for venue, minimum in [('lighter', Decimal(10)), ('arcus', Decimal(5))]:
            request.price_buy = request.price_sell = Decimal('148.66')
            field = 'price_buy' if venue == buy else 'price_sell'
            setattr(request, field, (minimum - Decimal('.01')) / request.quantity)
            with pytest.raises(ValueError): ArbitrageExecutor._validate_pair_quantity(executor, request)

@pytest.mark.parametrize('auth_source', ['env-only', 'template-env', 'yaml-only'])
def test_project_dotenv_reaches_native_factory_and_adapters(tmp_path, monkeypatch, auth_source):
    import socket
    from types import SimpleNamespace
    from dotenv import load_dotenv
    from core.adapters.exchanges.adapters import lighter
    from core.adapters.exchanges.adapters.arcus import ArcusAdapter
    from core.services.arbitrage_monitor_v2.core.orchestrator_bootstrap import OrchestratorBootstrap
    values = {
        'LIGHTER_API_KEY_PRIVATE_KEY': 'synthetic-$-test-only',
        'LIGHTER_ACCOUNT_INDEX': '7', 'LIGHTER_API_KEY_INDEX': '2',
        'ARCUS_WALLET_ADDRESS': '0xsynthetic-test-only',
        'ARCUS_API_KEY': 'synthetic-api-key',
        'ARCUS_API_SIGNING_KEY': 'synthetic-signing-key', 'ARCUS_ACCOUNT_INDEX': '3',
    }
    for key in list(__import__('os').environ):
        if key.startswith(('LIGHTER_', 'ARCUS_')):
            monkeypatch.delenv(key)
    templates = {name: yaml.safe_load(Path(f'examples/{name}_config.yaml.example').read_text())
                 for name in ('lighter', 'arcus')}
    monkeypatch.chdir(tmp_path)
    Path('logs').mkdir()
    if auth_source != 'env-only':
        (tmp_path / 'config/exchanges').mkdir(parents=True)
        if auth_source == 'yaml-only':
            templates['lighter']['api_config']['auth'].update(
                api_key_private_key=values['LIGHTER_API_KEY_PRIVATE_KEY'], account_index=7, api_key_index=2)
            templates['arcus']['arcus']['authentication'].update(
                wallet_address=values['ARCUS_WALLET_ADDRESS'], api_key=values['ARCUS_API_KEY'],
                api_signing_key=values['ARCUS_API_SIGNING_KEY'], account_index=3)
        for name, document in templates.items():
            Path(f'config/exchanges/{name}_config.yaml').write_text(yaml.safe_dump(document))
    if auth_source != 'yaml-only':
        Path('.env').write_text(''.join(f'{key}={value}\n' for key, value in values.items()))
        # Real dotenv semantics used by main_unified.py; never use a real credential file.
        for key in values:
            monkeypatch.setenv(key, '')
            monkeypatch.delenv(key)
        load_dotenv(Path('.env'), override=False, interpolate=False)
    def no_network(*args, **kwargs):
        raise AssertionError('configuration must not use network')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    # Keep the real factory, adapter constructors and configuration conversion;
    # replace only Lighter modules that could construct a native signer/transport.
    def module_stub(config):
        return SimpleNamespace(base_url='https://synthetic.invalid', ws_url='wss://synthetic.invalid')
    for name in ('LighterBase', 'LighterRest', 'LighterWebSocket'):
        monkeypatch.setattr(lighter, name, module_stub)
    monkeypatch.setattr(lighter.LighterAdapter, '_get_symbol_cache_service', lambda self: None)
    monkeypatch.setattr(lighter, 'create_subscription_manager', lambda **kwargs: None)
    orchestrator = SimpleNamespace(monitor_config=SimpleNamespace(exchanges=['lighter', 'arcus']), exchange_adapters={})
    OrchestratorBootstrap(orchestrator).init_exchange_adapters()
    assert set(orchestrator.exchange_adapters) == {'lighter', 'arcus'}
    lighter_adapter = orchestrator.exchange_adapters['lighter']
    assert isinstance(lighter_adapter, lighter.LighterAdapter)
    converted = lighter_adapter._config_dict
    assert converted['api_key_private_key'] == values['LIGHTER_API_KEY_PRIVATE_KEY']
    assert (converted['account_index'], converted['api_key_index']) == (7, 2)
    assert converted['auth_enabled'] is True
    assert lighter_adapter.strict_order_state is True
    arcus = orchestrator.exchange_adapters['arcus']
    assert isinstance(arcus, ArcusAdapter)
    assert arcus.address == values['ARCUS_WALLET_ADDRESS']
    assert arcus.api_key == values['ARCUS_API_KEY']
    assert arcus._seed == values['ARCUS_API_SIGNING_KEY']
    assert arcus.account_index == 3


def test_launcher_check_does_not_load_authentication_or_engine(tmp_path):
    import shutil
    import subprocess
    import sys
    shutil.copy('start.sh', tmp_path / 'start.sh')
    module = Path('core/services/arbitrage_monitor_v2/config/stock_preflight.py')
    (tmp_path / module).parent.mkdir(parents=True)
    shutil.copy(module, tmp_path / module)
    shutil.copytree('config/arbitrage', tmp_path / 'config/arbitrage')
    (tmp_path / '.env').mkdir()  # Reading this as dotenv would fail.
    (tmp_path / 'main_unified.py').write_text('raise AssertionError("engine imported")\n')
    result = subprocess.run(['bash', 'start.sh', 'check'], cwd=tmp_path,
                            env={'STOCK_PYTHON': sys.executable, 'PATH': '/usr/bin:/bin'},
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert 'no account or network checks performed' in result.stdout
    assert not (tmp_path / 'run').exists()
