"""gagelink: hydrology retrieval for AI agents.

Water data is published with everything an agent needs in order to use it correctly, in
that a discharge states its unit, a stage states the datum it is measured from, a reading
states whether the record is provisional or approved, and a timestamp states its offset.
Clients typically parse the number and drop the rest, which is where the errors come from.

This package retrieves against the USGS Water Data APIs and keeps that metadata attached,
using quantity-guard to enforce it at the tool boundary, and records each request in a form
that lets a session be re-run and its differences attributed.

Pre-alpha. The transport layer is present and the tool surface is not.
"""

from .service import (
    BASE_URL,
    COLLECTIONS,
    Cache,
    GagelinkError,
    MemoryCache,
    Quota,
    QuotaExhausted,
    Retrieval,
    Service,
    ServiceUnavailable,
    UnknownCollection,
)

#: Single source of truth for the version; pyproject reads it from here, so the packaged
#: metadata and the attribute cannot drift apart.
__version__ = "0.0.1"

__all__ = [
    "BASE_URL",
    "COLLECTIONS",
    "Cache",
    "GagelinkError",
    "MemoryCache",
    "Quota",
    "QuotaExhausted",
    "Retrieval",
    "Service",
    "ServiceUnavailable",
    "UnknownCollection",
    "__version__",
]
