# Known limitations

What is checked on hardware, and what does not work yet or is not verified,
in the current release. The release notes of each release say what is new on
this list.

## Growatt SPH10K

Tested in our test setup with an Edge101 connected to a real SPH10K inverter.

- **Checked with the firmware of September 2026:** reading the measurements,
  a restart of the controller while the inverter runs, and limiting the
  output power (active power rate).
- **Checked in spring 2026 with older firmware, not repeated since:** the
  export limit, the operating mode, the Grid First discharge power and stop
  SoC, and the Battery First charge stop SoC.
- **Not checked yet:** changing the Load First settings, the Battery First
  charge power and AC charging, and the Grid First time slots.

## Growatt MIC600

Supported by the configuration generator; not yet tested on hardware.

## Wi-Fi

2.4 GHz only, and the firmware has no fallback access point. Change the
network as the
[self-build guide](LOCAL-ESPHOME-SELF-BUILD.md#changing-wi-fi-later--plan-it-and-you-will-not-need-usb)
describes, or you need the USB cable again.

## Setup

- **The adoption asks for model, location and number before you select the
  controller.** It warns about a differing name only when it knows the name
  the controller calls itself. Compare the name and the MAC suffix in the
  device list yourself.

## Dashboards

- **An update does not rebuild a device dashboard.** When the release notes
  ask for it, rebuild it: **PVAutonomy** in the sidebar → **Maintenance** →
  **Refresh device dashboard**.
- **Refresh device dashboard shows no confirmation.** Open the device
  dashboard to see the result.
- **Grid First schedule on a controller set up earlier under another name.**
  If Home Assistant still lists the controller's controls under that earlier
  name, the schedule rows of the card *Grid First Settings* stay missing or
  unavailable, and *Activate* writes nothing and names the missing entity.
- **Removing the integration leaves its dashboards in the sidebar.** The
  *Uninstall* section of [INSTALLATION.md](INSTALLATION.md#uninstall) says how
  to delete them.
