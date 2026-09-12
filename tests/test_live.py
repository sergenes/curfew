"""Opt-in smoke test against the real router: uv run pytest -m live"""

from __future__ import annotations

import pytest

from curfew import actions
from curfew.config import MissingCredentials, Settings
from curfew.services import status as status_service
from curfew.soap import SoapClient

pytestmark = pytest.mark.live


@pytest.fixture
def settings() -> Settings:
    try:
        return Settings.from_env()
    except MissingCredentials:
        pytest.skip("no credentials in .env")


async def test_live_status_and_devices(settings: Settings) -> None:
    async with SoapClient(settings) as client:
        s = await status_service.get_status(client)
        assert s.info.model == "CAX80"
        assert s.device_count is not None and s.device_count > 0
        devices = await actions.get_attached_devices(client)
        assert all(d.mac.count(":") == 5 for d in devices)
