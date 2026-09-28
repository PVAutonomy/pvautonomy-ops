# Troubleshooting

Messages are quoted as the integration shows them in English. Text in
`{braces}` is filled in with your values.

When you report a problem, include the relevant lines of the Home Assistant
log for `pvautonomy_ops`, but **never** the content of your `secrets.yaml`,
an API key, or an OTA password.

---

## Setting up a new controller

### `secrets.yaml` could not be prepared

> Your ESPHome `secrets.yaml` could not be prepared, so the entries the build
> needs are not in place and there is no point generating a device YAML.
> Setup stopped here rather than report success. Nothing in that file was
> changed.
>
> Error: {error}
>
> What you can do: check that `/config/esphome/` is writable and has free
> space, and that `secrets.yaml` there is valid YAML — one `name: value` per
> line. With the File Editor add-on, Samba, or Terminal/SSH you can open it
> yourself. Then start the setup again.

The wizard does not repair or rewrite a `secrets.yaml` it cannot read; it
stops instead, so a file you can still fix by hand is left as it is. The
`{error}` part names the file and the reason, never a value from it. Typical
causes: a line that is not valid YAML, a value with a YAML tag the parser
cannot read, or a file that is not UTF-8.

### The device file already exists

> A file named `{node_name}.yaml` already exists in `/config/esphome/`.
> Setup stopped without touching it — it may be a device configuration
> you edited.
>
> What you can do: rename or remove the existing file in the ESPHome
> Device Builder, or run setup again with a different site or device
> number.

### The device YAML could not be generated or saved

> The device YAML for `{node_name}` could not be generated, so nothing was
> written and there is no file to build. Setup stopped here rather than
> report success.

> The device YAML for `{node_name}` was generated but could not be saved to
> `/config/esphome/`, so there is no file to build. Setup stopped here rather
> than report success.

Both messages continue with the error and what you can do. For the first,
check that you chose the right inverter model; for the second, that `/config`
is writable and has free space.

### The Wi-Fi screen does not accept the network name or password

> Enter the name of the 2.4 GHz Wi-Fi network the device should join.

> That is not a usable network name. An SSID is at most 32 characters and
> contains no control characters. Check it against what your router shows.

> Enter the password of that Wi-Fi network. An empty entry would be stored as
> an empty secret, and the build would fail on it.

> That is not a usable WPA2 passphrase. It must be 8 to 63 characters long and
> use only letters, digits, spaces and ordinary punctuation. If your network
> needs something else, write the entry into your ESPHome secrets.yaml
> yourself before running the wizard.

A control character in a network name is almost always left over from
copying it out of a router's web page; type the name instead. For an open
network, or a password the screen does not accept, write `wifi_ssid` and
`wifi_password` into `/config/esphome/secrets.yaml` yourself and run the
wizard again — it uses existing entries as they are and does not ask.

### "This setup path is not part of the PVAutonomy Community Alpha"

> This setup path is not part of the PVAutonomy Community Alpha. Use "Set up
> a new controller" or "Adopt a running controller".

You should not see this message on the normal path. It means a setup step
for a firmware service that this release does not offer was reached. Start
the setup again and choose one of the two entries.

---

## Building and flashing

### ESPHome reports that it is too old

> Your ESPHome version is too old. Please update to at least 2026.8.0

This message comes from ESPHome, not from PVAutonomy: the generated
configuration requires ESPHome 2026.8.0 or newer. Update the ESPHome Device
Builder add-on, or the ESPHome command-line tool, and validate again.

### USB flashing does not start from the Device Builder

Flashing from the Device Builder uses Web Serial, which only Chromium-based
browsers implement (Chrome, Edge, Chromium, Brave). In Safari or Firefox the
option to install over a cable attached to this computer does not work.

Web Serial also needs a secure page: Home Assistant opened over HTTPS, or on
`localhost`. Opened over plain `http://<address>:8123`, the Device Builder
sends you to ESPHome's own web flasher, web.esphome.io, with a message that
it cannot flash over USB because the page is not accessed over a secure
connection. You can flash from there, or open Home Assistant over HTTPS. The
ESPHome command-line tool needs no browser.

If the browser's port list does not change when you plug the controller in,
try a different USB cable: a cable that only charges shows no port. Details
are in [LOCAL-ESPHOME-SELF-BUILD.md](LOCAL-ESPHOME-SELF-BUILD.md).

### The serial console stays silent after the flash

That is expected. The generated firmware does not log to the serial port;
read the logs over the network with the **Logs** button in the Device Builder,
or `esphome logs <name>.yaml`.

---

## Adopting the running controller

### No device is offered

> No ESPHome devices with MAC addresses found. Please adopt your Edge101
> device in ESPHome first, then retry.

The controller has to be **added** to Home Assistant's ESPHome integration
first: **Settings → Devices & Services → ESPHome**. A device that is only
listed as *Discovered* is not added yet. If it never appears at all because
it sits on a VLAN, guest or IoT network, add it by IP address — see
[LOCAL-ESPHOME-SELF-BUILD.md](LOCAL-ESPHOME-SELF-BUILD.md), step 10b.

The list marks a controller with *— re-flash* when PVAutonomy already knows
it: from a PVAutonomy entry, or from an earlier setup whose entry has since
been removed. If a PVAutonomy entry is bound to it, it is not adopted a second
time: with the same location and number the setup ends with *"This device is
already configured."*; with a different location or number the target screen
shows *"Device slug is fixed after first install. …"*. A controller known only
from an earlier setup is adopted as usual; with different values, *Device
Already Known* asks first.

### The device lacks the expected entities

> The selected device does not expose the expected PVAutonomy ESPHome
> entities. Generate the device configuration again with "Set up a new
> controller", do not rename generated entities, flash the device again, wait
> until Home Assistant has created all ESPHome entities, then choose "Adopt a
> running controller" again.

Wait a minute after the controller comes online before you retry: Home
Assistant creates its entities once it has connected.

> The entity-surface contract could not be loaded for this model. The
> PVAutonomy integration may be incomplete or corrupted. Try reinstalling or
> updating the integration (through HACS or by hand), then choose "Adopt a
> running controller" again.

Reinstall the integration through HACS or by hand, restart Home Assistant,
and adopt again.

### The controller is already registered under another name

The screen **Device Already Known** shows the old and the new location and
number. **Relocate** moves the controller to the new values; **Cancel** ends
the setup with:

> Device is already bound to another configuration.

In the options (**Configure → Relocate Device**) a relocation can stop with:

> Relocate is temporarily disabled. The current implementation cannot
> guarantee ESPHome encryption continuity — the device would appear as newly
> discovered without its encryption key, which is a security risk. This will
> be resolved in a future update.

Nothing has been changed in that case. Keep the current location and number.

---

## Dashboards

### A device dashboard is missing or out of date

Open the System Dashboard (**PVAutonomy** in the sidebar) → **Maintenance**
→ **Refresh device dashboard** for the controller. The device dashboard is
rebuilt; changes you made to it by hand are replaced.

### The System Dashboard is gone

**Settings → Devices & Services → PVAutonomy → Configure → System Dashboard**
lets you turn it back on. It is rebuilt from your current controllers.
