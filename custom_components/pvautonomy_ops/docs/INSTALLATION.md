# Installation

PVAutonomy is a Home Assistant integration for a DFRobot Edge101 controller
(DFR0886) that connects to a Growatt inverter over RS485/Modbus.

You build and flash the controller's firmware yourself with ESPHome. The
integration prepares the device configuration for it and then adopts the
running controller.

PVAutonomy is an early release. What is checked on hardware, and what does
not work yet, is in [KNOWN-LIMITATIONS.md](KNOWN-LIMITATIONS.md). Project
status, license and security policy:
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
| HACS | for the installation through HACS: [HACS](https://hacs.xyz) installed in Home Assistant. Without HACS, install by hand (below) |

**Inverter status in this release:**

- **Growatt SPH10K** — tested in our test setup with an Edge101 connected to
  a real SPH10K inverter. What is checked there is listed in
  [KNOWN-LIMITATIONS.md](KNOWN-LIMITATIONS.md).
- **Growatt MIC600** — supported by the configuration generator; not yet
  tested on hardware.

No PVAutonomy account, key or online service is needed for anything in this
documentation.

---

## Install the integration

### With HACS (custom repository)

HACS must already be installed; its own guide is on [hacs.xyz](https://hacs.xyz).
The repository below is added **inside HACS**. The add-on store under
**Settings → Add-ons** (called *Apps* in newer Home Assistant versions) does
not accept it and reports that it is not a valid repository.

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

1. Read the release notes on
   [the releases page](https://github.com/PVAutonomy/pvautonomy-ops/releases):
   they say what changed and what to do after the update.
2. Install the update. With HACS, it appears under **Settings → Updates** as
   **PVAutonomy Ops**. Without HACS, replace the folder by hand as above.
3. Restart Home Assistant.
4. Reload the browser tab. If the setup still shows texts of the old version,
   do a hard reload (Ctrl+Shift+R, on macOS Cmd+Shift+R).

An update of the integration does not change the firmware on your controller.
If a release needs new firmware, its release notes say so, and you rebuild
and reflash it yourself as described in the self-build guide.

---

## Uninstall

1. **Settings → Devices & Services → PVAutonomy** → remove each entry.
2. Remove the dashboards PVAutonomy created. Removing the entries leaves
   them in the sidebar. Under **Settings → Dashboards** delete the System
   Dashboard **PVAutonomy** and the device dashboards named
   **PVAutonomy - …**.
3. Remove the integration in HACS, or delete the folder
   `/config/custom_components/pvautonomy_ops/`. The File editor add-on
   cannot delete a folder that still holds files. In a terminal (for
   example the Terminal & SSH add-on) run:

   ```
   rm -rf /config/custom_components/pvautonomy_ops
   ```

   Check the path before you press Enter: the command deletes without
   asking. In some terminal add-ons the folder is under `/homeassistant`
   instead of `/config`.
4. Restart Home Assistant.

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
- [KNOWN-LIMITATIONS.md](KNOWN-LIMITATIONS.md) — what is checked on hardware,
  and what does not work yet
- [FAQ.md](FAQ.md)
