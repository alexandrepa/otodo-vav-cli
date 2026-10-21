"""Unofficial Python client for the Otodo cloud API used by the VAV app."""

from .client import (
    BASE_URL,
    PILOT_WIRE_VALUE,
    HeatingMode,
    NotAuthenticatedError,
    Session,
    VavClient,
    VavError,
)

__all__ = [
    "BASE_URL",
    "PILOT_WIRE_VALUE",
    "HeatingMode",
    "NotAuthenticatedError",
    "Session",
    "VavClient",
    "VavError",
]
__version__ = "0.1.0"
