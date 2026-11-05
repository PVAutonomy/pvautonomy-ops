# PVAutonomy

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)

Read and control a Growatt inverter from Home Assistant with a DFRobot Edge101
controller (DFR0886) on RS485/Modbus. Its data goes straight from the inverter
to your Home Assistant — no cloud service, no Growatt server in between. The
setup wizard takes care of the device-specific part:

- It generates the ESPHome configuration for your controller and inverter
  model, and creates the controller's API key and OTA password on your own
  Home Assistant.
- You build and flash the firmware with ESPHome; the
  [self-build guide](custom_components/pvautonomy_ops/docs/LOCAL-ESPHOME-SELF-BUILD.md)
  takes you through it step by step.
- The integration then adopts the running controller and creates a dashboard
  for it.

## What works today

Tested in our test setup: an Edge101 connected to a real Growatt SPH10K, with
firmware from this integration's configuration generator (tier `extended`),
built with ESPHome 2026.8.2 in September 2026. Checked there:

- Reading the measurements: PV, AC output, battery, grid, load and energy
  counters
- Restarting the controller while the inverter runs
- Limiting the output power (active power rate)

Changing the other settings — export limit, operating mode, and the Battery
First, Load First and Grid First settings — is not yet checked with this
firmware.
[Known limitations](custom_components/pvautonomy_ops/docs/KNOWN-LIMITATIONS.md)
lists each of them.

**Growatt MIC600:** supported by the configuration generator; not yet tested
on hardware.

## Requirements

- A DFRobot Edge101 (DFR0886) and a Growatt SPH10K or MIC600.
- Home Assistant, and ESPHome **2026.8.0 or newer**: the ESPHome Device
  Builder add-on or the ESPHome command-line tool.
- A USB data cable for the first flash, and a 2.4 GHz Wi-Fi network.

The complete list is in the installation guide.

## Installation

With [HACS](https://hacs.xyz):

1. Open the repository in HACS with this button:

   [![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=PVAutonomy&repository=pvautonomy-ops&category=integration)

   Or add it by hand: **HACS** → three-dot menu → **Custom repositories** →
   `https://github.com/PVAutonomy/pvautonomy-ops`, type **Integration**;
   then search for **PVAutonomy** and open it.
2. Choose **Download**, and restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → PVAutonomy.**

Installing without HACS, and the steps after that: see the
[installation guide](custom_components/pvautonomy_ops/docs/INSTALLATION.md).

## Documentation

| Guide | What it covers |
|---|---|
| [Installation](custom_components/pvautonomy_ops/docs/INSTALLATION.md) | Install through HACS or by hand |
| [Setup wizard](custom_components/pvautonomy_ops/docs/SETUP-WIZARD.md) | Set up a controller in Home Assistant |
| [Local ESPHome self-build](custom_components/pvautonomy_ops/docs/LOCAL-ESPHOME-SELF-BUILD.md) | Build and flash the firmware yourself |
| [Known limitations](custom_components/pvautonomy_ops/docs/KNOWN-LIMITATIONS.md) | What is checked on hardware, and what does not work yet |
| [FAQ](custom_components/pvautonomy_ops/docs/FAQ.md) | Common questions |
| [Troubleshooting](custom_components/pvautonomy_ops/docs/TROUBLESHOOTING.md) | When something does not work |

## Support

- **Issues and bug reports:** [GitHub Issues](https://github.com/PVAutonomy/pvautonomy-ops/issues)
- **Community:** [Home Assistant Forum](https://community.home-assistant.io/)

## Project status

PVAutonomy is an early release. It comes without warranty and without a
support commitment, and no security assurance is given. What it was tested
on is listed under [What works today](#what-works-today).

Please report security problems privately, as described in
[SECURITY.md](SECURITY.md), and not in a public issue.

## License

Apache License 2.0; see [LICENSE](LICENSE).
