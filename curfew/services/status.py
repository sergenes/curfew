"""Router status: several read calls combined into one picture."""

from __future__ import annotations

import asyncio

from curfew import actions
from curfew.models import Band, RouterStatus, WlanInfo
from curfew.soap import SoapClient


async def get_status(client: SoapClient, *, with_device_count: bool = True) -> RouterStatus:
    info, uptime, system, wan, lan, access_control = await asyncio.gather(
        actions.get_router_info(client),
        actions.get_uptime(client),
        actions.get_system_info(client),
        actions.get_wan_info(client),
        actions.get_lan_info(client),
        actions.get_access_control_enabled(client),
    )
    device_count = None
    if with_device_count:
        device_count = len(await actions.get_attached_devices(client))
    return RouterStatus(
        info=info,
        uptime=uptime,
        system=system,
        wan=wan,
        lan=lan,
        access_control_enabled=access_control,
        device_count=device_count,
    )


async def get_wifi(client: SoapClient) -> list[WlanInfo]:
    return list(
        await asyncio.gather(
            actions.get_wlan_info(client, Band.GHZ_2_4),
            actions.get_wlan_info(client, Band.GHZ_5),
        )
    )
