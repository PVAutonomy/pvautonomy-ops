# PVAutonomy

A Home Assistant integration for a DFRobot Edge101 controller (DFR0886) that
reads and controls a Growatt inverter (SPH10K or MIC600) over RS485/Modbus.

This release is a **Community Alpha**: experimental, for technically capable
Home Assistant users, without warranty or support commitment. It does not
build or install firmware for you: the setup generates the ESPHome device
configuration, you build and flash it with ESPHome, and the setup then adopts
the running controller. What the project does and does not claim, and how to
report a security issue, is stated in the
[README](https://github.com/PVAutonomy/pvautonomy-ops) and the
[security policy](https://github.com/PVAutonomy/pvautonomy-ops/blob/main/SECURITY.md)
of the distribution repository.

## What it offers

- **Setup** (Settings → Devices & Services → Add Integration → PVAutonomy)
  with two entries:
  - *Set up a new controller* — generates the ESPHome device configuration in
    `/config/esphome/` and the four entries it needs in your ESPHome
    `secrets.yaml`;
  - *Adopt a running controller* — registers a controller that already runs
    the generated firmware. Nothing is built, installed or reflashed.
- **Options** of a controller: *Settings* (active device, poll interval),
  *System Dashboard* (remove it or bring it back), *Relocate Device*, and
  *Clean Up Old Entities*.
- **Grid Power** (optional), in the options of the *PVAutonomy Installation*
  entry: map a grid power sensor or a detected SHRDZM smart meter.
- **Dashboards**: a device dashboard per controller, created at adoption, and
  the System Dashboard (**PVAutonomy** in the sidebar) with a *Maintenance*
  view to refresh a device dashboard and a *Help* view.
- **Services**: `pvautonomy_ops.apply_noise_psk`,
  `pvautonomy_ops.set_selected_device`,
  `pvautonomy_ops.refresh_customer_dashboard` and
  `pvautonomy_ops.activate_grid_first_draft`.

## Quick start

1. Install the integration through HACS as a custom repository
   (`https://github.com/PVAutonomy/pvautonomy-ops`, type *Integration*) or
   by hand, then restart Home Assistant —
   [docs/INSTALLATION.md](docs/INSTALLATION.md).
2. Add the integration and choose **Set up a new controller**.
3. Build and flash the firmware yourself with ESPHome 2026.8.0 or newer —
   [docs/LOCAL-ESPHOME-SELF-BUILD.md](docs/LOCAL-ESPHOME-SELF-BUILD.md).
4. Add the integration again and choose **Adopt a running controller**.

## Documentation

- [Installation](docs/INSTALLATION.md) — requirements, install, update, uninstall
- [Setup wizard](docs/SETUP-WIZARD.md) — the setup, screen by screen
- [Local ESPHome self-build](docs/LOCAL-ESPHOME-SELF-BUILD.md) — building and flashing the firmware
- [Troubleshooting](docs/TROUBLESHOOTING.md) — messages you may see, and what to do
- [FAQ](docs/FAQ.md)

## Entity naming

The setup derives the controller's ESPHome node name from model, site and
device number, for example `mic600-garage-01`, and sets `esphome.friendly_name`
from it (`Mic600 Garage 01`). Home Assistant builds the prefix of every entity
ID from the friendly name, so the entities of that controller are named:

**Pattern:** `{domain}.{node_name with underscores}_{metric}_device`

| ESPHome node name | Home Assistant entity ID example |
|-------------------|----------------------------------|
| `mic600-garage-01` | `sensor.mic600_garage_01_energy_today_device` |
| `sph10k-home-05` | `sensor.sph10k_home_05_battery_soc_device` |

- Modbus sensor, number and switch entities carry the `_device` suffix, set in
  the entity `name:` of the generated YAML.
- System entities such as uptime, Wi-Fi signal and IP address have no
  `_device` suffix.
- `esphome.project.name: PVAutonomy.Edge101` is required: the integration
  finds its controllers by it.
- The PVAutonomy dashboards address entities by this prefix. Do not change
  `esphome.name` or `esphome.friendly_name` of a generated configuration;
  changing them after the first registration also leaves the old entity IDs
  in Home Assistant's entity registry. *Clean Up Old Entities* in the options
  finds this controller's entities whose ID ends in `_device` and whose
  prefix, up to the two-digit device number, differs from the current one,
  and can disable or delete them. Old IDs without that pattern it does not
  recognise.

## License

See the LICENSE file of the
[distribution repository](https://github.com/PVAutonomy/pvautonomy-ops).
