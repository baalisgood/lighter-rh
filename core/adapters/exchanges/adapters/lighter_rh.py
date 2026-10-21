"""
Lighter on Robinhood Chain ("Lighter RH") adapter.

Lighter RH reuses Lighter's transaction format, signer, REST API and WebSocket
channels, but it is a separate exchange: accounts, balances, API keys, nonces,
account indexes and market IDs do not carry over from Lighter mainnet.

Reference: https://apidocs.lighter.xyz/docs/lighter-rh
- REST:      https://api.rh.lighter.xyz  (/api/v1/)
- WebSocket: wss://api.rh.lighter.xyz/stream
- Chain ID:  466324
- Collateral: USDG
- API key indexes 0-3 and 157 are reserved by Lighter apps; use 4-156 or 158-254.

Credentials are read from LIGHTER_RH_API_KEY_PRIVATE_KEY, LIGHTER_RH_ACCOUNT_INDEX
and LIGHTER_RH_API_KEY_INDEX (or config/exchanges/lighter_rh_config.yaml).
"""

from .lighter import LighterAdapter
from .lighter_base import LighterBase


class LighterRHAdapter(LighterAdapter):
    """Lighter RH perpetuals (robinhoodchain.lighter.xyz)."""

    EXCHANGE_ID = "lighter_rh"
    CONFIG_FILE = "config/exchanges/lighter_rh_config.yaml"
    DEFAULT_API_URL = LighterBase.RH_MAINNET_URL
    DEFAULT_WS_URL = LighterBase.RH_MAINNET_WS_URL
    DEFAULT_CHAIN_ID = LighterBase.RH_MAINNET_CHAIN_ID
    # Keeps client order indexes disjoint from the mainnet adapter in the same process.
    CLIENT_ORDER_ID_OFFSET = 10_000_000_000_000
