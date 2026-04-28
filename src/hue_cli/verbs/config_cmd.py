"""Config verbs: ``config show`` / ``config validate`` (FR-64c).

These thinly wrap Engineer A's ``hue_cli.config`` module. ``show`` renders
the effective resolved config (post all overrides) in TOML format. ``validate``
loads and validates the config file at the given path (or default), exiting
0 on success or 6 (config error) on failure.

The functions imported from ``hue_cli.config`` are:

* ``show_effective(cfg: HueConfig) -> str`` — TOML rendering of the active config
* ``validate(path: Path) -> None`` — raises ``ConfigError`` on failure
* ``load_config(explicit_path: Path | None) -> HueConfig`` — resolution

Both are owned by Engineer A; this module is a verb façade only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click


@click.group(name="config")
def config_group() -> None:
    """Inspect and validate the hue-cli config file."""


@config_group.command("show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """Print the effective resolved config in TOML format (FR-64c)."""
    # Lazy import — Engineer A's `config` module may not exist on disk
    # during early parallel development; resolution is deferred to invocation
    # time so test_smoke.py and other unrelated commands still work.
    from hue_cli import config as cfg_mod  # local import

    obj = ctx.obj or {}
    active_cfg = obj.get("config") if isinstance(obj, dict) else None

    if active_cfg is None:
        # No pre-loaded config in the context — load fresh via the
        # resolution chain (--config / HUE_CLI_CONFIG / default).
        config_path = obj.get("config_path") if isinstance(obj, dict) else None
        path = Path(config_path).expanduser() if config_path else None
        active_cfg = cfg_mod.load_config(explicit_path=path)

    rendered = cfg_mod.show_effective(active_cfg)
    click.echo(rendered, nl=not rendered.endswith("\n"))


@config_group.command("validate")
@click.argument("path", required=False)
def config_validate(path: str | None) -> None:
    """Validate a config file at PATH (or the default location). Exits 6 on error."""
    from hue_cli import config as cfg_mod  # local import
    from hue_cli.config import DEFAULT_CONFIG_PATH  # local import
    from hue_cli.errors import HueCliError  # local import

    target = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH.expanduser()

    try:
        cfg_mod.validate(target)
    except HueCliError as exc:
        click.echo(f"config invalid: {exc}", err=True)
        sys.exit(getattr(exc, "exit_code", 6))
    except Exception as exc:
        click.echo(f"config invalid: {exc}", err=True)
        sys.exit(6)
    click.echo("config OK")
