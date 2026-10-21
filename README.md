# Lighter–Lighter RH (formerly Lighter–Arcus) stock perpetual arbitrage

MU, SNDK, AMD, NVDA, INTC and SPCX use the unified segmented engine with stock basis normalization, value-sized grids and the controls below. Defaults remain **monitor mode**. Offline tests do not certify account permissions, liquidity, profitability or live trading.

## Lighter 主网 ↔ Lighter Robinhood Chain（maker→taker，默认）

Arcus 已被 **Lighter on Robinhood Chain**（`lighter_rh`，<https://robinhoodchain.lighter.xyz>）替代为默认对手所；旧的 Lighter↔Arcus 配置仍保留，可用 `STOCK_VENUE=arcus bash start.sh ...` 启用。

- **执行模式**：`lighter_rh` 为 maker 腿（`order_mode: limit`、`maker_mode: true`、`post_only: true`，挂在 RH 最优买/卖价，被动价不劣于决策价）；成交后立即在 Lighter 主网用市价（taker）对冲。挂单超时（`limit_order_timeout`）或对冲所价格反向移动超过 `maker_cancel_adverse_pct` 时撤单，未成交不对冲；部分成交在满足最小量/最小名义金额时按成交量对冲。对冲保护价按实时价放宽 `hedge_max_slippage_pct`。
- **为什么这样分工**：RH 盘口较薄、价差较宽（适合挂单赚价差），主网盘口深（适合吃单对冲）。
- **配置文件**：`config/arbitrage/arbitrage_segmented_rh.yaml`、`arbitrage_lighter_rh.yaml`、`monitor_lighter_rh.yaml`、`multi_exchange_arbitrage_rh.yaml`；`bash start.sh check|monitor|live` 默认使用这些文件（`scripts/start_lighter_rh.sh` 等价）。
- **凭据**：`.env` 中填写 `LIGHTER_RH_API_KEY_PRIVATE_KEY`、`LIGHTER_RH_ACCOUNT_INDEX`、`LIGHTER_RH_API_KEY_INDEX`（与主网 `LIGHTER_*` 完全独立，不会互相回退）。可选 YAML 模板：`examples/lighter_rh_config.yaml.example`。
- **RH 要点**：REST `https://api.rh.lighter.xyz`，WS `wss://api.rh.lighter.xyz/stream`，chain ID `466324`；账户、API Key、nonce、market_id 均不与主网通用；API Key 索引 0–3 与 157 保留，请用 4–156 或 158–254；抵押品为 USDG（系统内仍按 USDC 计价）。
- **风险提示**：Standard 账户 REST 仅 60 次/分钟，且 maker 有 200ms 延迟、taker 300ms 延迟——订单状态轮询已限流，但仍建议先跑 `monitor`，必要时升级 Plus 档（手续费 0.005%/0.005%，需同步修改 `exchange_fees`）。基差 EMA 状态单独保存在 `data/stock_basis_state_lighter_rh.json`。阈值（T1=0.12% 等）按 Arcus 时代设定，费用归零后可在监控数据基础上下调。

### 增删交易对（BTC / ETH / SOL / HYPE 等）

`scripts/manage_symbols.py` 会一次性同步 4 个 RH 配置文件（策略、执行、监控、多所配对），自动从两所公开接口读取精度、最小下单量和最小名义金额，写入前先跑预检，失败不落盘：

```sh
.venv/bin/python scripts/manage_symbols.py list
.venv/bin/python scripts/manage_symbols.py add BTC ETH SOL HYPE           # 加密币：24x7，不使用美股时段阈值
.venv/bin/python scripts/manage_symbols.py add AAPL --stock               # 美股代币：沿用美股时段/盘后阈值
.venv/bin/python scripts/manage_symbols.py add BTC --replace --threshold 0.04 --step 0.02 --order-value 100
.venv/bin/python scripts/manage_symbols.py remove HYPE                     # 删除前先确认两所该币持仓已平
```

- 加密币默认 T1=0.05%、网格间距 0.03%（美股 0.12%/0.07%），请按 monitor 数据调整；改完需重启进程。
- 只有两所都上线的永续市场才能添加（例如 BNB 目前不在 RH 上，会被拒绝）。
- 交易对越多，RH Standard 档 60 次/分钟的 REST 额度越紧张，建议同时启用的品种不要太多。

## Install and run

Python 3.12 on macOS or Linux:

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-lighter-arcus.txt
bash start.sh check
cp -n .env.example .env
chmod 600 .env
# Fill .env locally with dedicated stock subaccount credentials.
bash start.sh monitor
# Explicit real trading:
bash start.sh live
```

`check` validates local profiles and decision/execution parity without credentials or network access. The direct `main_unified.py` entry and orchestrator constructor repeat this gate before loading credentials or connecting. Native `monitor` queries accounts but disables order submission. `start_stock_monitor.sh` aliases monitor; `scripts/start_lighter_arcus.sh` defaults to monitor. Set `STOCK_PYTHON` to an absolute interpreter path if needed.

Use the project-local `.env` as the primary authentication configuration: `LIGHTER_API_KEY_PRIVATE_KEY`, `LIGHTER_ACCOUNT_INDEX`, `LIGHTER_API_KEY_INDEX`, `ARCUS_WALLET_ADDRESS`, `ARCUS_API_KEY`, `ARCUS_API_SIGNING_KEY`, and `ARCUS_ACCOUNT_INDEX`. Enter the actual account and API key indices assigned to these credentials; do not assume an account index of `0`.

Authentication YAML is optional. For YAML-only configuration or advanced adapter settings, copy `examples/lighter_config.yaml.example` and `examples/arcus_config.yaml.example` into `config/exchanges/` without the `.example` suffix, protect them with `chmod 600`, and fill their empty fields locally. The existing loader gives environment credentials priority.

The wrapper clears inherited environment credentials, uses this project's local files, refuses symlinked runtime/authentication paths and holds a process lock. Templates contain no credentials. Do not copy the currency process's credentials. Shutdown uses the engine's emergency-close behavior; stopping a process does not guarantee flat accounts.

## Implemented controls

1. **Natural basis:** fresh Lighter/Arcus BBO mids produce `(midL - midA) / midA × 100`. Each symbol has a time-weighted 3,600-second EMA, sampled at most once per second. Maturity requires 3,600 accumulated seconds of valid paired samples; offline and invalid intervals never count. The one-hour drift comparison must remain within 0.10 percentage points. Missing, invalid or stale BBO immediately blocks openings. Interruptions up to 600 seconds preserve EMA, history and valid warmup progress; recovery requires 60 continuous seconds of fresh samples and the same drift check. More than 600 seconds since the last valid sample resets all progress. The first returning sample never weights the offline interval into EMA. The stock entrypoint checkpoints signal-only state every 60 seconds to ignored `data/stock_basis_state.json`, with atomic replacement; a compatible cache no older than 600 seconds restores progress but always requires fresh 60-second recovery. Invalid/incompatible caches fall back to cold warmup; cache I/O failures are logged without interrupting decisions. This behavior has offline coverage, not a one-hour live-feed validation.
2. **Price stability:** both execution prices must stay within 0.05% over two seconds, using the existing stability guard.
3. **Depth:** up to three levels are aggregated inside the BBO-based 0.01% per-leg protection band. Orders are rejected when protected depth cannot cover them. Depth is a snapshot, not a fill guarantee.
4. **First close:** `first_close_ratio: 0.5` sets T0 to half T1. Later grids close at the previous grid threshold. The engine, EMA gates and displays share this ratio; legacy profiles retain their previous 0.4 default.
5. **Funding:** cached rates up to 300 seconds old retain the adapter/UI eight-hour decimal contract and convert to hourly percent only at the decision boundary. For the configured one-hour holding forecast, only adverse `buy_rate - sell_rate` is deducted. Favorable funding is not prepaid income; missing/nonfinite/stale rates block openings when the funding guard is enabled. `enable_funding_rate_risk` controls this budget and the sustained-adverse guard. Fresh observations adverse by at least `funding_rate_diff_threshold` for `max_unfavorable_funding_hours` trigger the existing risk-close path. Unknown data, favorable rates and observation gaps reset that timer. This is an observed-rate risk estimate, not settled funding P&L or fetched payment history.
6. **Dollar sizing:** each grid targets 500 USDC per leg; each order targets at most 250 USDC per leg at its protected reference price. Quantity uses the larger executable leg price, reserves the execution protection allowance and floors to the common quantity step. A sell may receive price improvement and therefore settle above its reference notional; a sell limit cannot impose an upper fill price. There is no fixed-share fallback. Existing grid occupancy is `sum(remaining_quantity × max(actual_entry_buy, actual_entry_sell))`; price movements do not resize held grids. FIFO entry cost determines how many shares to close to a lower dollar grid. Partial fills record only actual fills. Close chunks avoid leaving an otherwise avoidable remainder below venue minima. An already existing remainder below verified shared minimum quantity/notional remains on the real ledger with an explicit manual-review block on that pair, including new openings. The engine never adds exposure to round up dust or silently deletes it; safe exact reduce-only closure requires confirmed support from both venues. Recovery uses recovered entry prices; missing cost blocks new exposure. Legacy fixed profiles remain supported.
7. **Profile parity:** both YAML files must agree on enabled symbols, fees, order modes, amount mode, grid/order amounts, precision and position caps before account access. Invalid/nonfinite effective per-symbol parameters, missing market constraints and conflicting fixed-quantity overrides are rejected. Protected stock execution supports market mode only; changing both profiles to limit mode is rejected. Legacy profiles retain their modes.
8. **Equity gap:** freshly validated account totals differing by at least 1,000 USDC produce a throttled reminder. This never transfers funds or independently blocks trading.
9. **Cash session:** `America/New_York` regular cash hours, 09:30–16:00, use each symbol's configured T1 (default 0.12%). Other times use 0.18%; step stays 0.07 percentage points. The configured 2026/2027 holidays and early closes apply; unknown years use off-hours thresholds. `closed_overrides` supports emergency closures. A threshold change resets signal persistence. Sessions raise opening thresholds; ordinary closes and risk exits remain available.
10. **Validation:** the public suite uses synthetic data and transports, checks runtime loading and parameter changes, and has no private sibling configuration dependency.

Selling Lighter subtracts basis from raw opening spread; buying Lighter adds it. Closing uses the original position direction, rather than its reversed execution roles or raw spread sign. Raw prices, raw spreads and FIFO costs remain available for execution/accounting. EMA warmup, drift and missing funding only gate openings. When basis is unavailable, ordinary close can use actual FIFO profit as a fallback; the execution layer still applies its FIFO net-profit guard. A moving EMA or a session threshold increase cannot authorize a loss-making ordinary close. Explicit risk exits retain their separate loss-reduction semantics.

## Configuration

Edit `config/arbitrage/arbitrage_segmented.yaml` and keep `arbitrage_lighter_arcus.yaml` execution fields consistent. `monitor_lighter_arcus.yaml` and `multi_exchange_arbitrage.yaml` select the six stocks. No extra or multi-leg symbols are enabled.

| Parameter | Current default |
| --- | ---: |
| Grid amount / maximum order per leg | 500 / 250 USDC |
| Maximum grids / each symbol per-venue cap | 3 / 1,500 USDC |
| Combined per-venue cap, shared by six stocks | 4,500 USDC |
| Dynamic cap | account total × 1.0 |
| Balance warning / block opening / force close | 4,000 / 4,000 / 3,000 USDC |
| Regular T1 / off-hours T1 / step | 0.12% / 0.18% / 0.07 percentage points |
| First close ratio | 0.5 |
| Basis EMA / warmup / drift lookback | 3,600 / 3,600 / 3,600 seconds |
| Maximum basis drift / quote gap | 0.10 percentage points / 3 seconds |
| Recovery / full reset / checkpoint | 60 / more than 600 / 60 seconds |
| Funding horizon / maximum cached rate age | 1 hour / 300 seconds |
| Sustained adverse funding threshold / duration | 0.01% hourly / 8 hours |
| Price stability / window | 0.05% / 2 seconds |
| Depth / per-leg protection | 3 levels / 0.01% |
| Price volatility pause | 4% in 300 seconds; pause 900 seconds |
| Equity gap reminder / throttle | 1,000 USDC / 300 seconds |

The combined cap is shared, not allocated equally; six separate 1,500-USDC allowances do not permit 9,000 USDC exposure. Effective exposure is bounded by the static cap, dynamic equity cap and other live guards. Nominal limits do not guarantee margin, capital utilization or maximum loss. Quantity steps and exchange minima remain in `market_constraints`. Adjust per-symbol T1/step/quantity/precision/risk values through symbol overrides; session exceptions and off-hours thresholds can also be overridden per symbol. Maintain the calendar before adding a new year.

## Quantitative expectations

Fee defaults assume **Lighter Standard 0 taker fee** and **Arcus Base 0.0225% taker fee**: round-trip fees are 0.045%. Verify actual tiers and update both tables. Fees are deducted before the opening grid threshold. At regular T1 0.12% and first-close ratio 0.5, ideal gross convergence capture after that opening fee budget is 0.06 percentage points. Subtracting four legs of 0.01% slippage leaves approximately **0.02% before funding**. A later grid's 0.07-point convergence leaves approximately **0.03% before funding**. Actual fills, changing basis, partial execution and funding can differ materially. `profit_per_segment` is not the principal unified-engine close trigger.

For hypothetical combined capital of 10,000 USDC, 20% simple APR means about 5.48 USDC/day. The previous 22,000-USDC daily volume per venue assumes **0.05% net per completed round**. Under 0.02% or 0.03% net assumptions, the corresponding volume is approximately **54,800 or 36,500 USDC per venue per day**, counting opens and closes. These are arithmetic scenarios, not forecasts or promised returns. There is no forced-volume controller or guaranteed 20% APR; evaluate realized net P&L, actual fees, settled funding and executed volume separately.

## Offline checks and sources

```sh
.venv/bin/pip install pytest pytest-asyncio
.venv/bin/python -m pytest tests -q
bash start.sh check
```

Tests use fake transports and synthetic credentials. Live account, long-duration streaming, recovery after a real partial fill and actual profitability require separate operational verification. No new production dependency is required. No source license grant was supplied; this release adds no license terms and retains third-party attribution. The existing Backpack documentation example remains synthetic.

Official references: [Lighter funding](https://docs.lighter.xyz/trading/funding), [Lighter fees](https://docs.lighter.xyz/trading/trading-fees), [Arcus fee tiers](https://api.arcus.xyz/v1/feetiers), [NYSE calendar](https://www.nyse.com/trade/hours-calendars), [Lighter markets](https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails), [Arcus markets](https://api.arcus.xyz/v1/markets).
