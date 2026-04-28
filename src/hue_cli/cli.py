"""Click entry point — fleshed out by the Phase 1 engineers."""

from __future__ import annotations

import click

from hue_cli import __version__


@click.group()
@click.version_option(version=__version__, prog_name="hue-cli")
def main() -> None:
    """hue-cli — deterministic local-LAN CLI for Philips Hue Bridges."""
