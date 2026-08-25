"""flightwatch - award availability monitoring with mileage thresholds."""

from __future__ import annotations

__version__ = "0.1.0"

from .models import Availability, Cabin, Deal, SearchQuery

__all__ = ["Availability", "Cabin", "Deal", "SearchQuery", "__version__"]
