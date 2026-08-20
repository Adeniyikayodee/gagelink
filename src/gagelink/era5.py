"""ERA5 reanalysis, from the Copernicus Climate Data Store.

Reanalysis is a modelled reconstruction of past weather on a global grid, which is where
precipitation and temperature come from for a catchment with no gauge in it. It arrives
through a queue rather than a request: a submission is accepted, runs for somewhere between
a minute and an hour, and produces a NetCDF or GRIB file.

That shape is why the request tools are named apart from the get tools. The distinction is
in `jobs.py` and the reason is there too.

An account is required, free from the data store, and nothing else in this package needs
one. Requests are refused here with an explanation rather than failing at the archive.
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping

from .jobs import Archive, Job

BASE_URL = "https://cds.climate.copernicus.eu/api/retrieve/v1"

#: The datasets a water question usually wants. The store publishes several hundred and
#: naming a few here is a starting point rather than a limit; any identifier it knows is
#: accepted.
DATASETS = {
    "reanalysis-era5-single-levels": (
        "Hourly surface fields from 1940 to five days ago, including total "
        "precipitation, 2 m temperature, and evaporation."
    ),
    "reanalysis-era5-land": (
        "Hourly land surface fields at finer resolution than the single levels, which is "
        "usually the one wanted for a catchment."
    ),
    "reanalysis-era5-land-monthly-means": "Monthly means of the land fields.",
}

#: Variables named as the store names them, since its spellings are not guessable and a
#: wrong one is only discovered when the job fails an hour later.
VARIABLES = {
    "precipitation": "total_precipitation",
    "temperature": "2m_temperature",
    "evaporation": "total_evaporation",
    "runoff": "runoff",
    "snowmelt": "snowmelt",
    "soil_moisture": "volumetric_soil_water_layer_1",
}

#: What the store publishes each variable in, which its responses do not state and which
#: is the whole reason this package exists. Total precipitation is in metres of water
#: equivalent accumulated over the hour, which reads as a plausible depth in the wrong
#: unit if taken for millimetres.
UNITS = {
    "total_precipitation": "meter",
    "2m_temperature": "kelvin",
    "total_evaporation": "meter",
    "runoff": "meter",
    "snowmelt": "meter",
    "volumetric_soil_water_layer_1": "dimensionless",
}


class Climate(Archive):
    """A client for the climate data store."""

    name = "cds"
    base_url = BASE_URL

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
        super().__init__(
            api_key or os.environ.get("CDS_API_KEY") or os.environ.get("CDSAPI_KEY"),
            **kwargs,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "PRIVATE-TOKEN": self.api_key or "",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _submit_request(
        self, dataset: str, request: Mapping[str, Any]
    ) -> tuple[int, str]:
        url = f"{self.base_url}/processes/{dataset}/execution"
        body = json.dumps({"inputs": dict(request)}).encode()
        status, _, text = self._send(url, "POST", self._headers(), body)
        return status, text

    def _status_request(self, job: Job) -> tuple[int, str]:
        status, _, text = self._send(
            f"{self.base_url}/jobs/{job.id}", "GET", self._headers(), None
        )
        return status, text

    def _read_status(self, job: Job, payload: Mapping[str, Any]) -> Job:
        """Update a job from a status document.

        The result link arrives under `links` rather than in the status body, so a job
        that reports success without one is reported as still running rather than as
        ready, which is the safer of the two wrong answers.
        """
        job.status = str(payload.get("status") or job.status).lower()
        job.message = payload.get("message") or job.message
        for link in payload.get("links") or []:
            if (link or {}).get("rel") in {"results", "result"}:
                job.href = link.get("href")
        if job.status == "successful" and not job.href:
            job.href = (payload.get("result") or {}).get("href") if isinstance(
                payload.get("result"), dict
            ) else None
        return job

    def describe(self, dataset: str) -> tuple[int, str]:
        """The store's own description of a dataset, which needs no account."""
        status, _, text = self._send(
            f"{self.base_url}/processes/{dataset}",
            "GET",
            {"Accept": "application/json"},
            None,
        )
        return status, text
