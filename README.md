# hue-cli

Deterministic, scriptable command-line tool for Philips Hue Bridges.

> **Status:** Phase 1 in development. See [`docs/SRD-hue-cli.md`](docs/SRD-hue-cli.md) for the full spec.

## Install

```bash
uv tool install git+ssh://git@github.com/agileguy/hue-cli
```

## Quick start

```bash
# Discover a bridge on the LAN
hue-cli bridge discover

# Pair (button-press flow)
hue-cli bridge pair --bridge-ip 192.168.86.62

# List rooms and lights
hue-cli list rooms
hue-cli list lights

# Turn off the kitchen
hue-cli off @kitchen
```

## Documentation

- [`docs/SRD-hue-cli.md`](docs/SRD-hue-cli.md) — software requirements document

## License

MIT — see [`LICENSE`](LICENSE).
