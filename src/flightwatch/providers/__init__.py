"""Award-availability providers."""

from __future__ import annotations

from .base import Provider, ProviderError
from .mock import MockProvider
from .seats_aero import SeatsAeroProvider

__all__ = ["MockProvider", "Provider", "ProviderError", "SeatsAeroProvider", "build_provider"]


def build_provider(config) -> Provider:  # noqa: ANN001 - avoids a circular import on ProviderConfig
    """Instantiate the provider named in config, validating credentials up front."""
    if config.kind == "mock":
        return MockProvider()
    if config.kind == "seats_aero":
        api_key = config.api_key()
        if not api_key:
            raise ProviderError(
                f"${config.api_key_env} is not set. Export your seats.aero Partner API key, "
                'or set provider.kind = "mock" to run against sample data.'
            )
        return SeatsAeroProvider(
            api_key=api_key,
            base_url=config.base_url,
            requests_per_minute=config.requests_per_minute,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            taxes_in_cents=config.taxes_in_cents,
            booking_url_template=config.booking_url_template,
        )
    raise ProviderError(f"Unknown provider kind {config.kind!r}")
