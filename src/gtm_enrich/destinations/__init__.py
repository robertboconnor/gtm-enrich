"""Destination registry -- add an entry here and the CLI can target it."""

from __future__ import annotations

from pathlib import Path

from .base import Destination, DestinationError
from .dryrun import DryRunDestination
from .hubspot import HubSpotDestination
from .salesforce import SalesforceDestination

DESTINATIONS = ("dryrun", "hubspot", "salesforce")


def build_destination(
    name: str,
    *,
    object_type: str,
    field_names: list[str],
    output_dir: Path,
    shape: str = "hubspot",
) -> Destination:
    if name == "dryrun":
        return DryRunDestination(output_dir=output_dir, shape=shape)
    if name == "hubspot":
        return HubSpotDestination(object_type=object_type, field_names=field_names)
    if name == "salesforce":
        return SalesforceDestination(object_type=object_type, field_names=field_names)
    raise DestinationError(f"Unknown destination '{name}'. Known: {', '.join(DESTINATIONS)}")


__all__ = [
    "Destination",
    "DestinationError",
    "DryRunDestination",
    "HubSpotDestination",
    "SalesforceDestination",
    "DESTINATIONS",
    "build_destination",
]
