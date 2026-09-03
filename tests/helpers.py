"""Small helpers shared by the test modules."""

from __future__ import annotations

from otto.config import ProviderConfig
from otto.providers.base import MockProvider
from otto.services import Services


def use_mock_provider(
    services: Services, *, scripted=None, tier: str = "both", **kw
) -> MockProvider:
    """Point one or both model tiers at a MockProvider and return it."""
    provider = MockProvider(scripted=dict(scripted or {}), **kw)
    tiers = ("fast", "strong") if tier == "both" else (tier,)
    for name in tiers:
        services.config.providers[name] = ProviderConfig(kind="mock", model="mock-1")
        services._provider_cache[name] = provider
    return provider
