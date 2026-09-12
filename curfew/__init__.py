"""Local control of a NETGEAR Nighthawk CAX80 over its SOAP API."""

from curfew.config import Settings
from curfew.soap import SoapClient, SoapError

__all__ = ["Settings", "SoapClient", "SoapError"]
