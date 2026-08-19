"""gagelink: hydrology retrieval for AI agents.

Water data is published with everything an agent needs in order to use it correctly, in
that a discharge states its unit, a stage states the datum it is measured from, a reading
states whether the record is provisional or approved, and a timestamp states its offset.
Clients typically parse the number and drop the rest, which is where the errors come from.

This package retrieves against the USGS Water Data APIs and keeps that metadata attached,
using quantity-guard to enforce it at the tool boundary, and records each request in a form
that lets a session be re-run and its differences attributed.

Pre-alpha. Transport, normalisation, and the USGS tools are present. Forecast and
network tools, the MCP server, and replay are not.
"""

#: Single source of truth for the version; pyproject reads it from here, so the packaged
#: metadata and the attribute cannot drift apart.
__version__ = "0.0.1"

from .normalise import (
    Location,
    Reading,
    UnitsNotPublished,
    UnknownUnit,
    grade,
    location_from,
    parse_unit,
    reading_from,
    readings_from,
    timezone_of,
)
from .nwps import Forecasts, Gauge, GaugeNotFound, Threshold
from .results import ErrorCode, Result
from .session import Session
from .tools import Toolkit
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

__all__ = [
    "BASE_URL",
    "ErrorCode",
    "Forecasts",
    "Gauge",
    "GaugeNotFound",
    "Threshold",
    "Result",
    "Session",
    "Toolkit",
    "Location",
    "Reading",
    "UnitsNotPublished",
    "UnknownUnit",
    "grade",
    "location_from",
    "parse_unit",
    "reading_from",
    "readings_from",
    "timezone_of",
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
