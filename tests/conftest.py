"""Shared fixtures: a fake router served by respx from recorded XML responses."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
import respx

from curfew.config import Settings
from curfew.soap import SoapClient

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = Settings(host="router.test", soap_port=5043, soap_ssl=True, user="admin", password="pw")

LOGIN_OK = """<?xml version="1.0" encoding="UTF-8"?>
<soap-env:Envelope xmlns:soap-env="http://schemas.xmlsoap.org/soap/envelope/">
<soap-env:Body>
    <m:SOAPLoginResponse xmlns:m="urn:NETGEAR-ROUTER:service:DeviceConfig:1"></m:SOAPLoginResponse>
    <ResponseCode>000</ResponseCode>
</soap-env:Body>
</soap-env:Envelope>"""

CODE_ONLY = """<?xml version="1.0" encoding="UTF-8"?>
<soap-env:Envelope xmlns:soap-env="http://schemas.xmlsoap.org/soap/envelope/">
<soap-env:Body>
    <ResponseCode>{code}</ResponseCode>
</soap-env:Body>
</soap-env:Envelope>"""


def fixture_text(name: str) -> str:
    return (FIXTURES / f"{name}.xml").read_text()


def code_only(code: str) -> str:
    return CODE_ONLY.format(code=code)


class FakeRouter:
    """Routes each SOAP action to a canned response and records what was called."""

    def __init__(self, mock: respx.MockRouter) -> None:
        self.mock = mock
        self.responses: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.calls: list[str] = []
        self.session_valid = True
        mock.post(SETTINGS.soap_url).mock(side_effect=self._dispatch)

    def serve(self, service: str, action: str, body: str, *, status: int = 200) -> None:
        self.responses[f"{service}#{action}"] = lambda _req: httpx.Response(status, text=body)

    def serve_fixture(self, service: str, action: str) -> None:
        self.serve(service, action, fixture_text(f"{service.split(':')[0]}_{action}"))

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        action = request.headers["SOAPAction"].removeprefix("urn:NETGEAR-ROUTER:service:")
        self.calls.append(action)
        if action == "DeviceConfig:1#SOAPLogin":
            body = request.content.decode()
            if "<Password>pw</Password>" not in body:
                return httpx.Response(200, text=code_only("401"))
            self.session_valid = True
            return httpx.Response(200, text=LOGIN_OK, headers={"Set-Cookie": "sess_id=abc123; Path=/"})
        if not self.session_valid or "sess_id=abc123" not in request.headers.get("Cookie", ""):
            return httpx.Response(200, text=code_only("401"))
        handler = self.responses.get(action)
        if handler is None:
            return httpx.Response(200, text=code_only("501"))
        return handler(request)


@pytest.fixture
def router() -> AsyncIterator[FakeRouter]:
    with respx.mock(assert_all_called=False) as mock:
        yield FakeRouter(mock)


@pytest.fixture
async def client(router: FakeRouter) -> AsyncIterator[SoapClient]:
    async with SoapClient(SETTINGS) as c:
        yield c


_ = re  # keep re available for tests that build patterns
