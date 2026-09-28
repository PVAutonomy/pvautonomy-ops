# Installation

PVAutonomy is a Home Assistant integration for a DFRobot Edge101 controller
(DFR0886) that connects to a Growatt inverter over RS485/Modbus. This release
is a **Community Alpha**: experimental, for technically capable Home
Assistant users, without any warranty or support commitment.

This release does not build or install firmware for you. You build and flash
the controller's firmware yourself with ESPHome, and the integration helps you
prepare it and then adopts the running controller.

What this release does not claim, and how to report a security issue, is
stated in the project README and security policy:
[README](https://github.com/PVAutonomy/pvautonomy-ops) ·
[SECURITY.md](https://github.com/PVAutonomy/pvautonomy-ops/blob/main/SECURITY.md).

---

## What you need

| | |
|---|---|
| Controller | DFRobot Edge101 (DFR0886) |
| Inverter | Growatt SPH10K or Growatt MIC600 (see below) |
| Home Assistant | with the **ESPHome Device Builder** add-on, or the ESPHome command-line tool on a computer to which you copy the device configuration and its four `secrets.yaml` entries (see the self-build guide, step 9) |
| ESPHome | **2026.8.0 or newer** — the generated device configuration requires it |
| Browser | a Chromium-based browser (Chrome, Edge, Chromium, Brave) if you flash from the Device Builder |
| Cable | a USB **data** cable for the first flash |
| Network | a 2.4 GHz Wi-Fi network — the controller has no 5 GHz radio |

**Inverter status in this release:**

- **Growatt SPH10K** — generated firmware has run on an Edge101 connected to
  an SPH10K in a bench setup.
- **Growatt MIC600** — supported by the configuration generator; not yet
  validated on hardware with this release.

No PVAutonomy account, key or online service is needed for anything in this
documentation.

---

## Install the integration

### With HACS (custom repository)

1. In Home Assistant open **HACS**.
2. Open the three-dot menu → **Custom repositories**.
3. Repository: `https://github.com/PVAutonomy/pvautonomy-ops`, type:
   **Integration**. Add it.
4. Search for **PVAutonomy**, open it and choose **Download**.
5. Restart Home Assistant.

### By hand

1. Open the release you want to install (normally the latest) on
   [the releases page](https://github.com/PVAutonomy/pvautonomy-ops/releases)
   and download **Source code (zip)** under **Assets**. GitHub builds this
   archive from the release tag; you do not need the other entries there.
2. Unpack the archive. It holds the whole repository in one folder. Copy the
   folder `custom_components/pvautonomy_ops/` from it into
   `/config/custom_components/` of your Home Assistant, so that
   `/config/custom_components/pvautonomy_ops/manifest.json` exists.
3. Restart Home Assistant.

---

## Add the integration

**Settings → Devices & Services → Add Integration → PVAutonomy.**

The setup offers two entries:

- **Set up a new controller** — prepares the device configuration and the
  credentials it needs; you then build and flash the firmware yourself.
- **Adopt a running controller** — registers a controller that already runs
  the generated firmware. Nothing is built, installed, or reflashed.

For a new controller you use both, in this order. The whole path is described
in [SETUP-WIZARD.md](SETUP-WIZARD.md) and, for the build and flash in the
middle, in [LOCAL-ESPHOME-SELF-BUILD.md](LOCAL-ESPHOME-SELF-BUILD.md).

---

## Update

Update through HACS, or replace the folder by hand as above, then restart
Home Assistant. Read the release notes on
[the releases page](https://github.com/PVAutonomy/pvautonomy-ops/releases)
before you update: they list what changed, including anything that was
removed.

An update of the integration does not change the firmware on your controller.
If a release needs new firmware, its release notes say so, and you rebuild
and reflash it yourself as described in the self-build guide.

---

## Uninstall

1. **Settings → Devices & Services → PVAutonomy** → remove each entry.
2. Remove the integration in HACS, or delete
   `/config/custom_components/pvautonomy_ops/`.
3. Restart Home Assistant.

The device configuration in `/config/esphome/` and the entries in your
ESPHome `secrets.yaml` belong to your ESPHome setup. Remove them yourself if
you no longer need them.

---

## Next

- [SETUP-WIZARD.md](SETUP-WIZARD.md) — the setup, screen by screen
- [LOCAL-ESPHOME-SELF-BUILD.md](LOCAL-ESPHOME-SELF-BUILD.md) — building and
  flashing the firmware
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — messages you may see, and what
  to do
- [FAQ.md](FAQ.md)
