"""Credentials store with strict 0600 enforcement and atomic write.

Per SRD §6.3 / FR-9 / FR-CRED-1..7. The on-disk schema is JSON:

    {
      "version": 1,
      "bridges": {
        "<canonical-12-char-id>": {
          "app_key": "<bridge-issued>",
          "host": "<ipv4>",
          "name": "<bridge-config-name>",
          "paired_at": "<iso8601>"
        }
      }
    }
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from hue_cli.errors import (
    ConfigError,
    HueCliError,
    NotPairedError,
)

CURRENT_VERSION = 1
DEFAULT_PATH = Path("~/.config/hue-cli/credentials")
ENV_VAR = "HUE_CLI_CREDENTIALS_FILE"


class CredentialsError(HueCliError):
    """Base class for credentials-store errors. Use subclasses for specific failures."""

    exit_code = 2
    error = "auth_failed"


class PermissiveCredentialsError(CredentialsError):
    """Credentials file mode is more permissive than 0600 (FR-CRED-2)."""


class MissingCredentialsError(NotPairedError):
    """Credentials file does not exist; verb requires a paired bridge (FR-CRED-3)."""


class UnknownVersionError(ConfigError):
    """Credentials ``version`` field is not the current schema version (FR-CRED-1)."""


@dataclass(frozen=True)
class BridgeCredentials:
    """One paired bridge's credentials. Bridge id is the credentials-dict key, not a field."""

    app_key: str
    host: str
    name: str
    paired_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "app_key": self.app_key,
            "host": self.host,
            "name": self.name,
            "paired_at": self.paired_at,
        }


@dataclass(frozen=True)
class CredentialsStore:
    """Top-level credentials document. Maps canonical 12-char bridge id to credentials."""

    bridges: dict[str, BridgeCredentials] = field(default_factory=dict)
    version: int = CURRENT_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "bridges": {bid: creds.to_dict() for bid, creds in self.bridges.items()},
        }


def credentials_path() -> Path:
    """Resolve the credentials file path: env var takes precedence over the default."""

    raw = os.environ.get(ENV_VAR)
    if raw:
        return Path(raw).expanduser()
    return DEFAULT_PATH.expanduser()


def load() -> CredentialsStore:
    """Read, validate mode 0600, validate schema, and return the store.

    Raises ``MissingCredentialsError`` (exit 2) if the file does not exist.
    Raises ``PermissiveCredentialsError`` (exit 2) if the file mode is more permissive than 0600.
    Raises ``UnknownVersionError`` (exit 6) if the ``version`` field is not the current version.
    """

    path = credentials_path()
    if not path.exists():
        raise MissingCredentialsError(
            f"no credentials file at {path}",
            hint="Run: hue-cli bridge pair",
        )

    _verify_mode_0600(path)

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CredentialsError(f"could not read credentials at {path}: {exc!r}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"credentials at {path} is not valid JSON: {exc.msg}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"credentials at {path} must be a JSON object, got {type(data).__name__}")

    version = data.get("version")
    if version is None:
        # SRD FR-CRED-1: missing version → treat as v1 with deprecation warning. The migrate
        # verb is the canonical fix. We do not warn here; auth migrate handles the messaging.
        version = CURRENT_VERSION
    if not isinstance(version, int) or version != CURRENT_VERSION:
        raise UnknownVersionError(
            f"credentials version {version!r} unknown (expected {CURRENT_VERSION})",
            hint="Run: hue-cli auth migrate",
        )

    bridges_raw = data.get("bridges", {})
    if not isinstance(bridges_raw, dict):
        raise ConfigError(
            f"credentials 'bridges' must be an object, got {type(bridges_raw).__name__}"
        )

    bridges: dict[str, BridgeCredentials] = {}
    for bid, body in bridges_raw.items():
        if not isinstance(body, dict):
            raise ConfigError(f"credentials.bridges.{bid} must be an object")
        try:
            bridges[bid] = BridgeCredentials(
                app_key=str(body["app_key"]),
                host=str(body["host"]),
                name=str(body["name"]),
                paired_at=str(body["paired_at"]),
            )
        except KeyError as exc:
            raise ConfigError(
                f"credentials.bridges.{bid} missing required key {exc.args[0]!r}"
            ) from exc

    return CredentialsStore(bridges=bridges, version=version)


def save(store: CredentialsStore) -> None:
    """Atomically write the store with mode 0600. Creates parent dir if absent."""

    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(store.to_dict(), indent=2, sort_keys=True) + "\n"

    fd, tmp_name = tempfile.mkstemp(prefix=".credentials.", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise


def append_bridge(bridge_id: str, creds: BridgeCredentials) -> None:
    """Load → mutate → save. Bridge id MUST be the canonical 12-char form."""

    try:
        store = load()
    except MissingCredentialsError:
        store = CredentialsStore()
    new_bridges = dict(store.bridges)
    new_bridges[bridge_id] = creds
    save(CredentialsStore(bridges=new_bridges, version=store.version))


def remove_bridge(bridge_id: str) -> bool:
    """Remove a bridge entry. Returns True if removed, False if not present."""

    try:
        store = load()
    except MissingCredentialsError:
        return False
    if bridge_id not in store.bridges:
        return False
    new_bridges = {bid: c for bid, c in store.bridges.items() if bid != bridge_id}
    save(CredentialsStore(bridges=new_bridges, version=store.version))
    return True


def flush_all() -> None:
    """Wipe the credentials file while preserving mode 0600 (FR-CRED-5)."""

    save(CredentialsStore(bridges={}, version=CURRENT_VERSION))


def _verify_mode_0600(path: Path) -> None:
    """Raise PermissiveCredentialsError if the file is group/world readable or writable."""

    info = path.stat()
    mode = stat.S_IMODE(info.st_mode)
    permissive = mode & (stat.S_IRWXG | stat.S_IRWXO)
    if permissive:
        raise PermissiveCredentialsError(
            f"credentials at {path} have mode {oct(mode)}; expected 0o600",
            hint=f"Run: chmod 600 {path}",
        )


__all__ = [
    "CURRENT_VERSION",
    "BridgeCredentials",
    "CredentialsError",
    "CredentialsStore",
    "MissingCredentialsError",
    "PermissiveCredentialsError",
    "UnknownVersionError",
    "append_bridge",
    "credentials_path",
    "flush_all",
    "load",
    "remove_bridge",
    "save",
]
