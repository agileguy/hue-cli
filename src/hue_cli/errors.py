"""Error taxonomy and structured stderr emission per SRD §11."""

from __future__ import annotations

import json
import sys
from typing import ClassVar


class HueCliError(Exception):
    """Base exception. All hue-cli errors derive from this and carry an exit code."""

    exit_code: ClassVar[int] = 1
    error: ClassVar[str] = "unknown_error"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class BridgeError(HueCliError):
    """Bridge returned a non-auth, non-network error response (v1 error type other than 1/101)."""

    exit_code: ClassVar[int] = 1
    error: ClassVar[str] = "unknown_error"


class AuthError(HueCliError):
    """Authentication failed: no app-key, app-key revoked, or link-button-not-pressed timeout."""

    exit_code: ClassVar[int] = 2
    error: ClassVar[str] = "auth_failed"


class NotPairedError(AuthError):
    """No paired bridge for the verb that requires one."""

    error: ClassVar[str] = "not_paired"


class LinkButtonNotPressedError(AuthError):
    """Pair flow exhausted its retry budget without the operator pressing the link button."""

    error: ClassVar[str] = "link_button_not_pressed"


class NetworkError(HueCliError):
    """Bridge unreachable, mDNS bind failed, NUPNP unreachable, or generic timeout."""

    exit_code: ClassVar[int] = 3
    error: ClassVar[str] = "bridge_unreachable"


class BridgeBusyError(HueCliError):
    """Bridge rate limit exhausted after aiohue's 25-retry budget."""

    exit_code: ClassVar[int] = 3
    error: ClassVar[str] = "bridge_busy"


class NotFoundError(HueCliError):
    """Light/room/zone/scene/sensor name not on bridge, or alias unknown."""

    exit_code: ClassVar[int] = 4
    error: ClassVar[str] = "unknown_target"


class AmbiguousTargetError(HueCliError):
    """Multiple resources match the target name."""

    exit_code: ClassVar[int] = 64
    error: ClassVar[str] = "ambiguous_target"


class UnsupportedError(HueCliError):
    """Capability mismatch: color on a non-color light, write on a read-only verb."""

    exit_code: ClassVar[int] = 5
    error: ClassVar[str] = "unsupported_feature"


class ConfigError(HueCliError):
    """Config file missing when requested, invalid TOML, or unresolved reference."""

    exit_code: ClassVar[int] = 6
    error: ClassVar[str] = "config_error"


class PartialBatchError(HueCliError):
    """Mixed-result batch run: at least one sub-op succeeded and at least one failed."""

    exit_code: ClassVar[int] = 7
    error: ClassVar[str] = "partial_failure"


class UsageError(HueCliError):
    """Invalid CLI invocation: ambiguous target, mutually-exclusive flags, bad enum."""

    exit_code: ClassVar[int] = 64
    error: ClassVar[str] = "usage_error"


class InterruptedError(HueCliError):
    """Process received SIGINT or SIGTERM during execution."""

    exit_code: ClassVar[int] = 130
    error: ClassVar[str] = "interrupted"


def emit_structured_error(
    err: HueCliError,
    *,
    target: str | None = None,
    json_mode: bool = False,
) -> None:
    """Write a §11.2 structured error object to stderr.

    Single-line JSON when stdout is JSON/JSONL or stderr is not a tty; pretty-indented when
    stderr is a tty for readability.
    """

    payload: dict[str, object] = {
        "error": err.error,
        "exit_code": err.exit_code,
        "target": target,
        "message": err.message,
    }
    if err.hint:
        payload["hint"] = err.hint

    interactive = sys.stderr.isatty() and not json_mode
    if interactive:
        sys.stderr.write(json.dumps(payload, indent=2) + "\n")
    else:
        sys.stderr.write(json.dumps(payload) + "\n")
