"""Runtime settings, loaded from environment variables and an optional .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    host: str = "192.168.1.1"
    soap_port: int = 5043
    soap_ssl: bool = True
    user: str = "admin"
    password: str = ""
    timeout_s: float = 30.0
    data_dir: Path = Path.home() / ".curfew"

    @property
    def registry_path(self) -> Path:
        return self.data_dir / "registry.db"

    @property
    def soap_url(self) -> str:
        scheme = "https" if self.soap_ssl else "http"
        return f"{scheme}://{self.host}:{self.soap_port}/soap/server_sa/"

    @classmethod
    def from_env(cls, dotenv_path: Path | None = None) -> Settings:
        """Read CURFEW_* variables (falling back to the older NIGHTHAWK_* names). Loads a .env if present."""
        load_dotenv(dotenv_path or _find_dotenv(), override=False)
        password = _env("PASSWORD", "")
        if not password:
            raise MissingCredentials("CURFEW_PASSWORD is not set (put it in .env or the environment)")
        return cls(
            host=_env("HOST", cls.host),
            soap_port=int(_env("SOAP_PORT", str(cls.soap_port))),
            soap_ssl=_env("SOAP_SSL", "true").lower() in {"1", "true", "yes"},
            user=_env("USER", cls.user),
            password=password,
            timeout_s=float(_env("TIMEOUT_S", str(cls.timeout_s))),
            data_dir=Path(_env("DATA_DIR", str(cls.data_dir))).expanduser(),
        )


class MissingCredentials(RuntimeError):
    pass


def _env(name: str, default: str) -> str:
    """Prefer CURFEW_<name>, fall back to the legacy NIGHTHAWK_<name>, then the default."""
    return os.environ.get(f"CURFEW_{name}") or os.environ.get(f"NIGHTHAWK_{name}") or default


def _find_dotenv() -> Path | None:
    """Walk up from the current directory and the package directory looking for a .env file."""
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        for directory in (start, *start.parents):
            candidate = directory / ".env"
            if candidate.is_file():
                return candidate
    return None
