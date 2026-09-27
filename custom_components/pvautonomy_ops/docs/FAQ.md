# Frequently asked questions

### What is PVAutonomy?

A Home Assistant integration for a DFRobot Edge101 controller (DFR0886) that
reads and controls a Growatt inverter over RS485/Modbus. The controller runs
ESPHome firmware that is generated for your inverter model.

### What does "Community Alpha" mean?

This release is experimental and meant for technically capable Home
Assistant users. It comes without warranty and without a support
commitment. What the project does and does not claim is stated in the
[README](https://github.com/PVAutonomy/pvautonomy-ops) and the
[security policy](https://github.com/PVAutonomy/pvautonomy-ops/blob/main/SECURITY.md).

### Which inverters are supported?

- **Growatt SPH10K** — generated firmware has run on an Edge101 connected to
  an SPH10K in a bench setup.
- **Growatt MIC600** — supported by the configuration generator; not yet
  validated on hardware with this release.

The setup offers no other models.

### Does the integration build or install the firmware?

No. This release does not build or install firmware for you. The setup
writes the device configuration into `/config/esphome/`, and you build and
flash it with the ESPHome Device Builder add-on or the ESPHome command-line
tool. See [LOCAL-ESPHOME-SELF-BUILD.md](LOCAL-ESPHOME-SELF-BUILD.md).

### Which ESPHome version do I need?

2026.8.0 or newer. The generated configuration sets this as its minimum, so
an older ESPHome refuses to compile it.

### Do I need an account, a key, or a PVAutonomy online service?

No. Nothing in this documentation needs one.

### Is HACS required?

No. You can install through HACS as a custom repository, or copy the
integration folder by hand. See [INSTALLATION.md](INSTALLATION.md).

### Where are my Wi-Fi password and the device credentials stored?

In your ESPHome `secrets.yaml`, `/config/esphome/secrets.yaml`, on your Home
Assistant. The setup writes only entries that are missing: your Wi-Fi name
and password as you typed them, and an API encryption key and an OTA password
it generates for the controller. It never changes an entry that is already
there and never writes a value to the log. The device configuration refers
to these entries with `!secret` and contains no credential value itself.
Details: [SETUP-WIZARD.md](SETUP-WIZARD.md#what-the-wizard-writes).

### Can I use more than one controller?

Yes. Give each one a different location or device number during setup. Each
controller gets its own API key and OTA password; the Wi-Fi entries are
shared.

### Can I change the Wi-Fi network later?

Yes, but plan it: the firmware has no fallback access point. Change the
entries in `secrets.yaml`, rebuild, and install over the network **while the
controller is still on the old network**; only then switch the network. See
[LOCAL-ESPHOME-SELF-BUILD.md](LOCAL-ESPHOME-SELF-BUILD.md#changing-wi-fi-later--plan-it-and-you-will-not-need-usb).

### Can I edit the generated device configuration?

Some parts, yes. The self-build guide lists what is safe to change and what
breaks the integration's view of the device.

### Where do I report a problem?

In the [issue tracker](https://github.com/PVAutonomy/pvautonomy-ops/issues).
Security issues follow the
[security policy](https://github.com/PVAutonomy/pvautonomy-ops/blob/main/SECURITY.md)
instead. Never post the content of your `secrets.yaml`, an API key or an OTA
password.
