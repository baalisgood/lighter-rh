"""Venue identity helpers shared by adapters and the arbitrage engine."""

# Exchanges served by the Lighter adapter family (same signer, API and WS protocol).
# Each is a separate exchange with its own accounts, nonces and market IDs.
LIGHTER_FAMILY = frozenset({"lighter", "lighter_rh"})


def is_lighter_family(exchange_name) -> bool:
    """True for Lighter mainnet and Lighter on Robinhood Chain."""
    return str(exchange_name or "").lower() in LIGHTER_FAMILY
