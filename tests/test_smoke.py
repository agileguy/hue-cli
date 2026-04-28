"""Smoke test — confirms the CLI imports and reports its version."""

from click.testing import CliRunner

from hue_cli import __version__
from hue_cli.cli import main


def test_version() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_help() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "hue-cli" in result.output
