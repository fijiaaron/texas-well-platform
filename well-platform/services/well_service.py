"""Business logic sitting above WellRepository. This is what a future API
layer (REST, GraphQL, whatever) calls — it should never need to import
data.orm or write SQL of its own; if it needs a new query shape, that shape
belongs in WellRepository, not here.
"""

from __future__ import annotations

from domain.enums import OperationalStatus
from domain.models import Well, WellSearchQuery, WellSearchResult
from domain.status_mapping import DEFAULT_OPERATIONAL_STATUS_ON_ENROLLMENT

from data.repositories.well_repository import WellRepository


class WellNotFoundError(Exception):
    pass


class AlreadyEnrolledError(Exception):
    pass


class WellService:
    def __init__(self, well_repository: WellRepository):
        self.well_repository = well_repository

    def get_well(self, api_number: str) -> Well:
        well = self.well_repository.get_by_api_number(api_number)
        if well is None:
            raise WellNotFoundError(api_number)
        return well

    def search(self, query: WellSearchQuery) -> WellSearchResult:
        return self.well_repository.search(query)

    def list_monitoring_candidates(
        self, county_name: str | None = None, limit: int = 100, offset: int = 0
    ) -> WellSearchResult:
        """Wells worth pointing a Sigil Node at: RRC-orphaned or
        plugged/abandoned, and not already in our monitoring program. This
        is the query that drives POC-03 site selection.
        """
        return self.well_repository.search_monitoring_candidates(
            county_name=county_name, limit=limit, offset=offset
        )

    def enroll_well_for_monitoring(
        self, api_number: str, status: OperationalStatus | None = None
    ) -> Well:
        """Marks a well as part of the monitoring program — call this when a
        Sigil Node is physically deployed. If `status` isn't given, it's
        inferred from the well's regulatory status (see
        domain/status_mapping.py DEFAULT_OPERATIONAL_STATUS_ON_ENROLLMENT),
        falling back to ACTIVE.
        """
        well = self.get_well(api_number)
        if well.is_monitored:
            raise AlreadyEnrolledError(
                f"{api_number} is already enrolled (operational_status={well.operational_status})"
            )
        resolved = status or DEFAULT_OPERATIONAL_STATUS_ON_ENROLLMENT.get(
            well.regulatory_status, OperationalStatus.ACTIVE
        )
        self.well_repository.set_operational_status(well.id, resolved)
        well.operational_status = resolved
        return well

    def update_operational_status(self, api_number: str, status: OperationalStatus) -> Well:
        """For status transitions on an already-enrolled well — e.g. a field
        crew arriving (-> INSPECTION) or a node going quiet (-> OFFLINE).
        Leak-triggered transitions to LEAKING normally come from
        SensorReadingService.record_readings, not this method.
        """
        well = self.get_well(api_number)
        if not well.is_monitored:
            raise ValueError(f"{api_number} is not enrolled in monitoring — call enroll_well_for_monitoring first")
        self.well_repository.set_operational_status(well.id, status)
        well.operational_status = status
        return well

    def decommission_well(self, api_number: str) -> Well:
        """Removes a well from the monitoring program (node pulled, program
        ended for that site) without deleting its RRC regulatory history.
        """
        well = self.get_well(api_number)
        self.well_repository.set_operational_status(well.id, None)
        well.operational_status = None
        return well
