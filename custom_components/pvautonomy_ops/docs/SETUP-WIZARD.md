# Setup wizard

This page walks through the PVAutonomy setup screen by screen. Screen titles
and messages are quoted as the integration shows them in English.

This release does not build or install firmware for you. Setting up a new
controller therefore runs the wizard **twice**: once to prepare the device
configuration, and — after you have built and flashed the firmware yourself —
once more to adopt the running controller.

```
Set up a new controller  →  build and flash with ESPHome  →  Adopt a running controller
      (wizard, part 1)        (LOCAL-ESPHOME-SELF-BUILD.md)        (wizard, part 2)
```

Start: **Settings → Devices & Services → Add Integration → PVAutonomy.**

---

## Part 1 — Set up a new controller

The first screen, **PVAutonomy Setup**, offers two entries. Choose
**Set up a new controller**.

1. **Build Firmware with ESPHome** — explains what follows. Submit it.
2. **Select Model** — *Growatt SPH10K* or *Growatt MIC600*. (A manufacturer
   screen exists, but it is skipped while Growatt is the only manufacturer.)
3. **Device Location** — a location (*Haus / Home*, *Garage*,
   *Garten / Garden*, or *Custom...* with your own name of at least 2
   characters) and a **device number** from 1 to 10. Together with the
   model they form the controller's name, for example `sph10k-home-02`.
   Write these values down: you enter the same ones again in part 2.
4. **Device already exists** — appears only if a device with the same name is
   already in Home Assistant. Continuing means building firmware for exactly
   that device; for a new device, go back and choose a different location or
   number.
5. **Wi-Fi for the device** — appears only if your ESPHome `secrets.yaml`
   has no `wifi_ssid` or no `wifi_password` yet. Enter the name and password
   of your 2.4 GHz network. See "What the wizard writes" below.
6. **Ready to Flash** — the device configuration has been written to
   `/config/esphome/<name>.yaml`, and the ESPHome Device Builder already lists
   it. Submitting this screen ends the wizard with the message
   *"Local ESPHome YAML exported. Build and flash the device with ESPHome,
   then return to register the running device."* No PVAutonomy entry exists
   yet at this point; that is expected.

Now build and flash the firmware:
[LOCAL-ESPHOME-SELF-BUILD.md](LOCAL-ESPHOME-SELF-BUILD.md). Come back when
the controller runs and Home Assistant has **added** it in the ESPHome
integration (not only discovered it).

### What the wizard writes

Before it writes the device configuration, the wizard makes sure that your
ESPHome `secrets.yaml` (`/config/esphome/secrets.yaml`) holds the four
entries the configuration refers to:

| Entry | Where the value comes from |
|---|---|
| `wifi_ssid` | what you typed, stored exactly as entered |
| `wifi_password` | what you typed, stored exactly as entered |
| `api_encryption_key_<name>` | generated on your Home Assistant: 32 random bytes, Base64 |
| `ota_password_<name>` | generated on your Home Assistant: 16 random bytes, 32 hex characters |

- An entry that is already in the file is never changed, regenerated or
  overwritten. If all four are present, the file is not written at all.
- New entries are appended below a comment line; the rest of the file keeps
  its exact content, comments and formatting.
- The file is kept readable by its owner only (mode `0600`).
- The wizard logs the names of the entries it adds, never their values.
- The two generated entries carry the controller's name, so every controller
  gets its own pair. The two Wi-Fi entries are shared by all controllers on
  your network.

The device configuration itself contains no credential value: it refers to
these four entries with `!secret`.

The Wi-Fi screen accepts a network name of at most 32 characters without
control characters, and a password of 8 to 63 printable ASCII characters. For
an open network, or a password outside that range, write the two Wi-Fi
entries into `secrets.yaml` yourself before you run the wizard; it then uses
them as they are.

---

## Part 2 — Adopt a running controller

**Before you start:** the controller must be running and **added** in the
ESPHome integration (**Settings → Devices & Services → ESPHome**). A
controller that is only listed as discovered there is not offered by this
wizard. To add it, see step 11 of
[LOCAL-ESPHOME-SELF-BUILD.md](LOCAL-ESPHOME-SELF-BUILD.md).

Start the wizard again and choose **Adopt a running controller**.

1. **Select Model** and **Device Location** — the same values as in part 1.
2. **Select Target Device** — the ESPHome devices Home Assistant knows that
   have a MAC address. Choose your controller. The list marks a controller
   with *— re-flash* when PVAutonomy already knows it: from a PVAutonomy
   entry, or from an earlier setup whose entry has since been removed. If a
   PVAutonomy entry is bound to it, it is not adopted a second time: with the
   same location and number the setup ends with *"This device is already
   configured."*; with a different location or number the target screen
   shows *"Device slug is fixed after first install. …"*. A controller known
   only from an earlier setup is adopted as usual; with different values,
   *Device Already Known* asks first.

   Each entry shows the controller's name and the last six characters of
   its MAC address. Choose the controller you set up in part 1; the list
   may hold other controllers. If yours is missing, cancel, add it in the
   ESPHome integration as described above, and start again.
3. **Controller Name Differs** — appears only if the controller you
   selected calls itself differently from the name that your model,
   location and number give, for example `sph10k-home-02` instead of
   `sph10k-bench-05`. **Cancel** stops the setup without registering
   anything: start again and select the right controller, or enter its
   location and number. **Adopt anyway** continues: use it only when you
   deliberately give this controller a new location or number.
4. **Device Already Known** — appears only if no PVAutonomy entry is bound to
   this controller, but PVAutonomy still knows it from an earlier setup under
   a different model, location or number. **Relocate** moves it to the new
   values; **Cancel** stops the setup.
5. **Adopt Running Device** — shows what will be registered. Submit it.
   Before it registers the controller, the wizard checks that the device
   offers the entities the generated firmware provides, and derives the
   feature level from them. No firmware is built, installed, or reflashed.

After that, the controller appears under **Settings → Devices & Services →
PVAutonomy**, and a device dashboard is created in the background. The
System Dashboard — **PVAutonomy** in the sidebar — lists your controllers;
its **Maintenance** view has a **Refresh device dashboard** button for each of
them.

---

## Options

**Settings → Devices & Services → PVAutonomy → Configure** offers:

| Entry | What it does |
|---|---|
| **Settings** | *Active device* and *Poll interval (seconds)* |
| **Relocate Device** | change the location and number of this controller |
| **Clean Up Old Entities** | find this controller's entities that still carry an earlier `<model>_<site>_<nn>` prefix, then disable or delete them |
| **System Dashboard** | remove the System Dashboard, or bring it back |

---

If a screen shows a message you do not expect, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).
