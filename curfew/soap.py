"""Minimal async client for the NETGEAR router SOAP API.

The router speaks a home grown SOAP dialect:
- every call is a POST to /soap/server_sa/ with a SOAPAction header
- login is DeviceConfig:1#SOAPLogin, which returns a sess_id cookie
- every response carries a <ResponseCode>; 000 is success, 401 means login again
- writes must be wrapped in ConfigurationStarted / ConfigurationFinished
"""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Self

import httpx

from curfew.config import Settings

log = logging.getLogger(__name__)

SERVICE_PREFIX = "urn:NETGEAR-ROUTER:service:"
# The routers accept any fixed session id in the header; this is the one every client uses.
SESSION_ID = "A7D88AE69687E58D9A00"
OK_CODES = frozenset({"0", "000", "0000"})

_ENVELOPE = """<?xml version="1.0" encoding="utf-8" standalone="no"?>
<SOAP-ENV:Envelope xmlns:SOAPSDK1="http://www.w3.org/2001/XMLSchema"
  xmlns:SOAPSDK2="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:SOAPSDK3="http://schemas.xmlsoap.org/soap/encoding/"
  xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">
<SOAP-ENV:Header>
<SessionID>{session_id}</SessionID>
</SOAP-ENV:Header>
<SOAP-ENV:Body>
<M1:{action} xmlns:M1="{service}">
{params}</M1:{action}>
</SOAP-ENV:Body>
</SOAP-ENV:Envelope>
"""

_CODE_RE = re.compile(r"<ResponseCode>\s*(\w+)\s*</ResponseCode>")
_CODE_MEANING = {
    "401": "unauthorized",
    "402": "missing parameter",
    "404": "service not found",
    "501": "action not supported",
    "503": "router busy",
}


class SoapError(RuntimeError):
    """A SOAP call did not succeed."""

    def __init__(self, service: str, action: str, code: str, raw: str = "") -> None:
        meaning = _CODE_MEANING.get(code, "error")
        super().__init__(f"{service}#{action} failed: ResponseCode {code} ({meaning})")
        self.service = service
        self.action = action
        self.code = code
        self.raw = raw

    @property
    def unsupported(self) -> bool:
        return self.code in {"404", "501"}


class LoginError(SoapError):
    pass


@dataclass
class SoapResponse:
    service: str
    action: str
    code: str
    raw: str
    _root: ET.Element | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return self.code in OK_CODES

    @property
    def root(self) -> ET.Element:
        if self._root is None:
            self._root = ET.fromstring(self.raw)
        return self._root

    @property
    def body(self) -> ET.Element | None:
        """The <ActionResponse> element, ignoring namespaces."""
        wanted = f"{self.action}Response"
        return next((el for el in self.root.iter() if local_name(el.tag) == wanted), None)

    def fields(self) -> dict[str, str]:
        """Direct children of the response element as tag -> text."""
        body = self.body
        if body is None:
            return {}
        return {local_name(child.tag): (child.text or "").strip() for child in body if not len(child)}

    def value(self, name: str) -> str:
        value = self.fields().get(name)
        if value is None:
            raise KeyError(f"{self.service}#{self.action}: field {name!r} missing in response")
        return value


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _extract_code(text: str) -> str:
    match = _CODE_RE.search(text)
    return match.group(1) if match else "missing"


class SoapClient:
    """One authenticated session with the router. Use as an async context manager."""

    def __init__(self, settings: Settings, *, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._http = http or httpx.AsyncClient(verify=False, timeout=settings.timeout_s)
        self._owns_http = http is None
        self._cookie: str | None = None
        self._lock = asyncio.Lock()
        self._in_config = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # -- low level -------------------------------------------------------

    async def _post(self, service: str, action: str, params: Mapping[str, str] | None) -> SoapResponse:
        body = "".join(f"<{k}>{_escape(v)}</{k}>\n" for k, v in (params or {}).items())
        message = _ENVELOPE.format(
            session_id=SESSION_ID, service=SERVICE_PREFIX + service, action=action, params=body
        )
        headers = {
            "SOAPAction": f"{SERVICE_PREFIX}{service}#{action}",
            "Cache-Control": "no-cache",
            "User-Agent": "curfew",
            # Yes, the router wants this content type for XML. Copied from every working client.
            "Content-Type": "multipart/form-data",
        }
        if self._cookie:
            headers["Cookie"] = self._cookie
        resp = await self._http.post(self.settings.soap_url, headers=headers, content=message)
        text = resp.text
        if resp.status_code != 200:
            raise SoapError(service, action, f"http{resp.status_code}", text)
        return SoapResponse(service, action, _extract_code(text), text)

    async def login(self) -> None:
        self._cookie = None
        resp = await self._post(
            "DeviceConfig:1",
            "SOAPLogin",
            {"Username": self.settings.user, "Password": self.settings.password},
        )
        if not resp.ok:
            raise LoginError("DeviceConfig:1", "SOAPLogin", resp.code, resp.raw)
        # httpx already stored the cookie in its jar, but the router wants it verbatim.
        cookie = self._http.cookies.get("sess_id")
        if not cookie:
            raise LoginError("DeviceConfig:1", "SOAPLogin", "nocookie", resp.raw)
        self._cookie = f"sess_id={cookie}"
        log.debug("logged in to %s", self.settings.soap_url)

    async def call(self, service: str, action: str, params: Mapping[str, str] | None = None) -> SoapResponse:
        """Call an action, logging in first or again when the session is missing or expired."""
        async with self._lock:
            try:
                return await self._call_locked(service, action, params)
            except httpx.TransportError as err:
                # The router's TLS listener resets connections now and then; one retry clears it.
                log.debug("transport error on %s#%s (%s), retrying once", service, action, err)
                await asyncio.sleep(1)
                self._cookie = None
                return await self._call_locked(service, action, params)

    async def _call_locked(self, service: str, action: str, params: Mapping[str, str] | None) -> SoapResponse:
        if self._cookie is None:
            await self.login()
        resp = await self._post(service, action, params)
        if resp.code == "401":
            log.debug("session expired, logging in again")
            await self.login()
            resp = await self._post(service, action, params)
        if not resp.ok:
            raise SoapError(service, action, resp.code, resp.raw)
        return resp

    # -- configuration mode ---------------------------------------------

    @asynccontextmanager
    async def config_mode(self) -> AsyncIterator[None]:
        """Wrap writes in ConfigurationStarted / ConfigurationFinished as the router requires.

        Nested use is a no-op so a service can compose several writes in one transaction.
        """
        if self._in_config:
            yield
            return
        await self.call("DeviceConfig:1", "ConfigurationStarted", {"NewSessionID": SESSION_ID})
        self._in_config = True
        try:
            yield
        finally:
            self._in_config = False
            try:
                await self.call("DeviceConfig:1", "ConfigurationFinished", {"NewStatus": "ChangesApplied"})
            except SoapError as err:
                # A reboot ends the session before ConfigurationFinished can be acknowledged.
                log.warning("ConfigurationFinished failed: %s", err)


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
