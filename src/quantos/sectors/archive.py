"""Daily SectorMembership snapshot creation from already PIT-resolved inputs."""

from __future__ import annotations

from datetime import date
from typing import Sequence

from quantos.schemas import SectorMembership, SectorMembershipSnapshot


def create_sector_membership_snapshot(
    memberships: Sequence[SectorMembership],
    *,
    as_of_date: date,
    historical_membership_mode: str = "strict_pit",
) -> list[SectorMembershipSnapshot]:
    """Create daily archive rows without inferring any past membership state."""

    rows = [
        SectorMembershipSnapshot(
            as_of_date=as_of_date,
            symbol=membership.symbol,
            sector_code=membership.sector_code,
            sector_name=membership.sector_name,
            classification=membership.classification,
            source=membership.source,
            available_at=membership.available_at,
            historical_membership_mode=historical_membership_mode,
        )
        for membership in memberships
    ]
    return sorted(rows, key=lambda row: row.symbol)
