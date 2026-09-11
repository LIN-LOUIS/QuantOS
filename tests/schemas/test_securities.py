from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from quantos.schemas import SecurityMaster


def test_security_master_uses_canonical_symbol() -> None:
    with pytest.raises(ValueError, match="canonical"):
        SecurityMaster(
            symbol="SZ000001",
            company_name="平安银行",
            exchange="SZSE",
            effective_from=date(1991, 4, 3),
            available_at=datetime.now(ZoneInfo("Asia/Shanghai")),
        )
