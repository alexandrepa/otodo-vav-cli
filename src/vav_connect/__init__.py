"""Unofficial Python client for the Otodo cloud API used by the VAV app."""

from .client import (
    BASE_URL,
    PILOT_WIRE_VALUE,
    TEMPERATURE_CAPABILITY_ID,
    HeatingMode,
    NotAuthenticatedError,
    Session,
    VavClient,
    VavError,
    kelvin_x10_to_celsius,
)

__all__ = [
    "BASE_URL",
    "PILOT_WIRE_VALUE",
    "TEMPERATURE_CAPABILITY_ID",
    "HeatingMode",
    "NotAuthenticatedError",
    "Session",
    "VavClient",
    "VavError",
    "kelvin_x10_to_celsius",
]
__version__ = "0.1.0"
