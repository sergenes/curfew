"""SoapClient: login, session refresh, error mapping, config mode."""

from __future__ import annotations

import pytest

from curfew.soap import LoginError, SoapClient, SoapError
from tests.conftest import SETTINGS, FakeRouter, code_only, fixture_text


async def test_login_happens_on_first_call(client: SoapClient, router: FakeRouter) -> None:
    router.serve_fixture("DeviceInfo:1", "GetSysUpTime")
    resp = await client.call("DeviceInfo:1", "GetSysUpTime")
    assert resp.ok
    assert resp.value("SysUpTime") == "4 days 14:57:40"
    assert router.calls == ["DeviceConfig:1#SOAPLogin", "DeviceInfo:1#GetSysUpTime"]


async def test_expired_session_triggers_one_relogin(client: SoapClient, router: FakeRouter) -> None:
    router.serve_fixture("DeviceInfo:1", "GetSysUpTime")
    await client.call("DeviceInfo:1", "GetSysUpTime")
    router.session_valid = False  # router forgot us
    router.calls.clear()
    resp = await client.call("DeviceInfo:1", "GetSysUpTime")
    assert resp.ok
    assert router.calls == [
        "DeviceInfo:1#GetSysUpTime",
        "DeviceConfig:1#SOAPLogin",
        "DeviceInfo:1#GetSysUpTime",
    ]


async def test_wrong_password_raises_login_error(router: FakeRouter) -> None:
    bad = SETTINGS.__class__(**{**SETTINGS.__dict__, "password": "nope"})
    async with SoapClient(bad) as client:
        with pytest.raises(LoginError) as err:
            await client.call("DeviceInfo:1", "GetSysUpTime")
    assert err.value.code == "401"


async def test_unsupported_action_raises_with_code(client: SoapClient, router: FakeRouter) -> None:
    router.serve_fixture("AdvancedQoS:1", "GetQoSEnableStatus")
    with pytest.raises(SoapError) as err:
        await client.call("AdvancedQoS:1", "GetQoSEnableStatus")
    assert err.value.code == "501"
    assert err.value.unsupported
    assert "action not supported" in str(err.value)


async def test_http_error_is_soap_error(client: SoapClient, router: FakeRouter) -> None:
    router.serve("DeviceInfo:1", "GetSysUpTime", "boom", status=500)
    with pytest.raises(SoapError) as err:
        await client.call("DeviceInfo:1", "GetSysUpTime")
    assert err.value.code == "http500"


async def test_config_mode_brackets_writes_and_nests(client: SoapClient, router: FakeRouter) -> None:
    router.serve("DeviceConfig:1", "ConfigurationStarted", code_only("000"))
    router.serve("DeviceConfig:1", "ConfigurationFinished", code_only("000"))
    router.serve("DeviceConfig:1", "SetBlockDeviceEnable", code_only("000"))
    # entered twice on purpose: the inner one must not start a second transaction
    async with client.config_mode(), client.config_mode():
        await client.call("DeviceConfig:1", "SetBlockDeviceEnable", {"NewBlockDeviceEnable": "1"})
    assert router.calls == [
        "DeviceConfig:1#SOAPLogin",
        "DeviceConfig:1#ConfigurationStarted",
        "DeviceConfig:1#SetBlockDeviceEnable",
        "DeviceConfig:1#ConfigurationFinished",
    ]


async def test_params_are_xml_escaped(client: SoapClient, router: FakeRouter) -> None:
    seen: list[str] = []

    def capture(request):  # type: ignore[no-untyped-def]
        seen.append(request.content.decode())
        return __import__("httpx").Response(200, text=code_only("000"))

    router.responses["DeviceInfo:1#SetNetgearDeviceName"] = capture
    await client.call("DeviceInfo:1", "SetNetgearDeviceName", {"NewDeviceName": "Tom & Jerry <TV>"})
    assert "<NewDeviceName>Tom &amp; Jerry &lt;TV&gt;</NewDeviceName>" in seen[0]


async def test_transport_error_is_retried_once(client: SoapClient, router: FakeRouter) -> None:
    import httpx

    attempts = {"n": 0}
    good = fixture_text("DeviceInfo_GetSysUpTime")

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("connection reset", request=request)
        return httpx.Response(200, text=good)

    router.responses["DeviceInfo:1#GetSysUpTime"] = flaky
    resp = await client.call("DeviceInfo:1", "GetSysUpTime")
    assert resp.ok and attempts["n"] == 2
    assert router.calls.count("DeviceConfig:1#SOAPLogin") == 2  # session re-established after the reset


async def test_transport_error_twice_propagates(client: SoapClient, router: FakeRouter) -> None:
    import httpx

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset", request=request)

    router.responses["DeviceInfo:1#GetSysUpTime"] = broken
    with pytest.raises(httpx.ConnectError):
        await client.call("DeviceInfo:1", "GetSysUpTime")
