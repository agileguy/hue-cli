"""TOML config loader per SRD §9 / FR-64.

Resolution order:
1. Explicit ``--config <path>`` argument (passed via ``load_config(explicit_path=...)``).
2. ``HUE_CLI_CONFIG`` environment variable.
3. ``~/.config/hue-cli/config.toml``.

Missing default file → built-in defaults, no error (FR-64b).
Missing explicit file → ``ConfigError`` exit code 6 (FR-64a).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from hue_cli.credentials import MissingCredentialsError, credentials_path
from hue_cli.credentials import load as load_credentials
from hue_cli.errors import ConfigError

DEFAULT_CONFIG_PATH = Path("~/.config/hue-cli/config.toml")
ENV_VAR = "HUE_CLI_CONFIG"


@dataclass(frozen=True)
class DefaultsSection:
    """[defaults] block. Mirrors SRD §9.2 with Decision 2 / 6 baked in."""

    bridge: str | None = None
    timeout_seconds: int = 5
    concurrency: int = 5
    output_format: str = "auto"
    cloud_discovery: bool = False
    transition_ms: int | None = None


@dataclass(frozen=True)
class CredentialsSection:
    """[credentials] block."""

    file_path: str = "~/.config/hue-cli/credentials"


@dataclass(frozen=True)
class LoggingSection:
    """[logging] block."""

    file: str | None = None


@dataclass(frozen=True)
class BridgeBlock:
    """[bridges.<alias>] block."""

    id: str | None = None
    host: str | None = None
    app_key_file: str | None = None


@dataclass(frozen=True)
class HueConfig:
    """Effective configuration after resolution."""

    defaults: DefaultsSection = field(default_factory=DefaultsSection)
    credentials: CredentialsSection = field(default_factory=CredentialsSection)
    logging: LoggingSection = field(default_factory=LoggingSection)
    bridges: dict[str, BridgeBlock] = field(default_factory=dict)
    source_path: Path | None = None

    @property
    def cloud_discovery(self) -> bool:
        return self.defaults.cloud_discovery

    @property
    def concurrency(self) -> int:
        return self.defaults.concurrency

    @property
    def timeout_seconds(self) -> int:
        return self.defaults.timeout_seconds


def resolve_path(explicit_path: Path | None) -> tuple[Path | None, bool]:
    """Return (path, was_explicit) where path is None if only the default applies and absent."""

    if explicit_path is not None:
        return explicit_path.expanduser(), True
    env_value = os.environ.get(ENV_VAR)
    if env_value:
        return Path(env_value).expanduser(), True
    default = DEFAULT_CONFIG_PATH.expanduser()
    if default.exists():
        return default, False
    return None, False


def load_config(explicit_path: Path | None = None) -> HueConfig:
    """Load and parse the effective config. Raises ``ConfigError`` per FR-64a/c."""

    path, explicit = resolve_path(explicit_path)
    if path is None:
        # FR-64b: missing default file is not an error. Built-in defaults stand in.
        return HueConfig(source_path=None)

    if not path.exists():
        if explicit:
            raise ConfigError(
                f"config file not found at {path}",
                hint=f"Create {path} or unset {ENV_VAR}.",
            )
        # TOCTOU: default file vanished between resolve_path and now — fall back silently.
        return HueConfig(source_path=None)

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"could not read config at {path}: {exc!r}") from exc

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML at {path}: {exc!s}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"config at {path} is not valid UTF-8: {exc!s}") from exc

    return _build_config(data, source=path)


def _build_config(data: dict[str, Any], *, source: Path) -> HueConfig:
    """Translate parsed TOML into a HueConfig. Raises ConfigError on shape errors."""

    defaults = _build_defaults(data.get("defaults", {}))
    creds = _build_credentials(data.get("credentials", {}))
    logging = _build_logging(data.get("logging", {}))
    bridges = _build_bridges(data.get("bridges", {}))

    return HueConfig(
        defaults=defaults,
        credentials=creds,
        logging=logging,
        bridges=bridges,
        source_path=source,
    )


def _build_defaults(section: object) -> DefaultsSection:
    if not isinstance(section, dict):
        raise ConfigError(f"[defaults] must be a table, got {type(section).__name__}")
    return DefaultsSection(
        bridge=_opt_str(section, "bridge"),
        timeout_seconds=int(section.get("timeout_seconds", 5)),
        concurrency=int(section.get("concurrency", 5)),
        output_format=str(section.get("output_format", "auto")),
        cloud_discovery=bool(section.get("cloud_discovery", False)),
        transition_ms=_opt_int(section, "transition_ms"),
    )


def _build_credentials(section: object) -> CredentialsSection:
    if not isinstance(section, dict):
        raise ConfigError(f"[credentials] must be a table, got {type(section).__name__}")
    return CredentialsSection(
        file_path=str(section.get("file_path", "~/.config/hue-cli/credentials")),
    )


def _build_logging(section: object) -> LoggingSection:
    if not isinstance(section, dict):
        raise ConfigError(f"[logging] must be a table, got {type(section).__name__}")
    return LoggingSection(file=_opt_str(section, "file"))


def _build_bridges(section: object) -> dict[str, BridgeBlock]:
    if not isinstance(section, dict):
        raise ConfigError(f"[bridges] must be a table, got {type(section).__name__}")
    out: dict[str, BridgeBlock] = {}
    for alias, body in section.items():
        if not isinstance(body, dict):
            raise ConfigError(f"[bridges.{alias}] must be a table, got {type(body).__name__}")
        out[alias] = BridgeBlock(
            id=_opt_str(body, "id"),
            host=_opt_str(body, "host"),
            app_key_file=_opt_str(body, "app_key_file"),
        )
    return out


def _opt_str(section: dict[str, Any], key: str) -> str | None:
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"key {key!r} must be a string, got {type(value).__name__}")
    return value


def _opt_int(section: dict[str, Any], key: str) -> int | None:
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, int):
        raise ConfigError(f"key {key!r} must be an integer, got {type(value).__name__}")
    return value


def show_effective(config: HueConfig) -> str:
    """Render the effective config as TOML (FR-64c)."""

    lines: list[str] = []
    lines.append("# Effective hue-cli configuration")
    if config.source_path is not None:
        lines.append(f"# source: {config.source_path}")
    else:
        lines.append("# source: built-in defaults (no config file found)")
    lines.append("")

    defaults = asdict(config.defaults)
    lines.append("[defaults]")
    for key, value in defaults.items():
        if value is None:
            continue
        lines.append(f"{key} = {_toml_literal(value)}")
    lines.append("")

    creds = asdict(config.credentials)
    lines.append("[credentials]")
    for key, value in creds.items():
        lines.append(f"{key} = {_toml_literal(value)}")
    lines.append("")

    log = asdict(config.logging)
    if any(v is not None for v in log.values()):
        lines.append("[logging]")
        for key, value in log.items():
            if value is None:
                continue
            lines.append(f"{key} = {_toml_literal(value)}")
        lines.append("")

    for alias, block in config.bridges.items():
        lines.append(f"[bridges.{alias}]")
        for key, value in asdict(block).items():
            if value is None:
                continue
            lines.append(f"{key} = {_toml_literal(value)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def validate(path: Path) -> None:
    """Load the config at ``path`` and check that every [bridges.<alias>] resolves.

    Resolution rule (§9.4): every [bridges.<alias>] entry MUST have a matching credentials
    entry whose canonical 12-char id equals the block's ``id`` field. Missing references are
    a config error.
    """

    config = load_config(explicit_path=path)

    if not config.bridges:
        return

    try:
        store = load_credentials()
    except MissingCredentialsError:
        # No paired bridges yet — any [bridges.<alias>] is dangling.
        dangling = list(config.bridges.keys())
        raise ConfigError(
            f"config references bridges {dangling!r} but no credentials are paired",
            hint="Run: hue-cli bridge pair",
        ) from None

    paired_ids = set(store.bridges.keys())
    for alias, block in config.bridges.items():
        if block.id is None:
            raise ConfigError(
                f"[bridges.{alias}] missing required key 'id' (canonical 12-char form)"
            )
        if block.id not in paired_ids:
            raise ConfigError(
                f"[bridges.{alias}] id={block.id!r} not in credentials at {credentials_path()}"
            )


def _toml_literal(value: object) -> str:
    """Render a Python scalar as a TOML literal. Limited to the types this config uses."""

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise ConfigError(f"cannot render {type(value).__name__} as TOML literal")


__all__ = [
    "ENV_VAR",
    "BridgeBlock",
    "CredentialsSection",
    "DefaultsSection",
    "HueConfig",
    "LoggingSection",
    "load_config",
    "resolve_path",
    "show_effective",
    "validate",
]
