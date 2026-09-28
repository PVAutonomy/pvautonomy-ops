# Local ESPHome self-build

This guide is the setup path of the PVAutonomy **Community Alpha**, for
technically capable Home Assistant users who build and flash the Edge101
firmware themselves with ESPHome.

The PVAutonomy setup generates the ESPHome device configuration and the
credentials it needs; you compile and flash it yourself; then the setup
adopts the running controller. This release does not build or install
firmware for you, so the manual steps below are the path, not a workaround.

**Requirements for this path:**
- ESPHome basic knowledge
- ESPHome **≥ 2026.8.0** — the generated device YAML uses the 2026.8
  `modbus_controller` API (read-back after every control write) and paces
  Modbus via `modbus.turnaround_time`; it has been compiled with 2026.8.x.
- The ESPHome Device Builder add-on — the wizard writes the device YAML into the
  directory it manages, so you no longer need file access for the YAML itself.
  File Editor, Samba, or Terminal/SSH are only needed if you maintain
  `secrets.yaml` by hand instead of editing it in the Device Builder.
  Alternatively, the ESPHome command-line tool on a computer: then you copy
  the device YAML and its four `secrets.yaml` entries to that computer, see
  step 9.
- **A Chromium-based browser, if you flash from the Device Builder** — Chrome,
  Edge, Chromium or Brave. Its USB flash runs on Web Serial, which Safari and
  Firefox do not implement. The ESPHome CLI flashes the serial port directly
  and needs no browser at all. See "Before you flash: the browser decides"
  below.
- PVAutonomy installed through HACS or by hand — see
  [INSTALLATION.md](INSTALLATION.md)

No PVAutonomy account, key or online service is involved at any step.

---

## When to use this path

**This path fits you if:**
- You installed PVAutonomy
- You want to build and flash the firmware yourself
- You understand ESPHome basics and can edit YAML and `secrets.yaml`
- You can maintain your ESPHome `secrets.yaml` — in the ESPHome Device Builder
  add-on, or with File Editor, Samba, or Terminal/SSH if you keep it by hand
- You can connect the device over USB to flash it the first time

**This path does not fit you if** you expect the integration to build or install
firmware for you. It does not, and no alternative in-wizard path currently does.

---

## End-to-end flow

```
1.  Install PVAutonomy (INSTALLATION.md).
2.  Start: Settings → Devices & Services → Add Integration → PVAutonomy.
3.  Select the first menu entry:
        "Set up a new controller".
4.  Read the "Build Firmware with ESPHome" screen and submit it, then follow
    the wizard:
      → Select model (Growatt SPH10K or Growatt MIC600)
      → Enter site name and device number
    A manufacturer screen sits in front of the model screen, but the wizard
    shows it only once a second manufacturer exists. Growatt is currently
    the only one, so you will not see that screen.
5.  PVAutonomy generates device YAML and writes it straight into the ESPHome
    directory:
        /config/esphome/<node_name>.yaml
6.  The "Ready to Flash" screen hands you over to the Device Builder.
    Submitting it closes the wizard (no config entry is created yet).
    The screen does not print the file path, because the Device Builder
    path does not need it. If you build from the CLI, the path is
    /config/esphome/<node_name>.yaml — see "Example generated filename".
7.  Nothing to do — the wizard already put the four required secrets in
    your ESPHome secrets.yaml (see Required ESPHome secrets below). If any
    were missing it generated the two credentials and asked you once for
    your Wi-Fi network; entries that were already there were not touched.
8.  Open the ESPHome Device Builder add-on. It already lists the device:
    the YAML sits in the directory the add-on manages, so there is nothing
    to copy or paste.
9.  Validate before you install. On the device's card: ⋮ → Validate, then
    ⋮ → Install, and in the ESPHome dialog choose the first option — the one
    that installs over the cable attached to this computer. The first flash
    has to go over USB. Flashing from the
    Device Builder needs a Chromium-based browser — read "Before you
    flash: the browser decides" below before you start. The ESPHome CLI
    does the same two steps without a browser:
        esphome config <node_name>.yaml
        esphome run <node_name>.yaml
    Run them in a directory that holds a copy of
    /config/esphome/<node_name>.yaml and a secrets.yaml with the four
    entries copied from /config/esphome/secrets.yaml: wifi_ssid,
    wifi_password, api_encryption_key_<node_name> and
    ota_password_<node_name>. The generated file refers to nothing else.
10. Wait until the ESPHome device appears in Home Assistant under
    Settings → Devices & Services → ESPHome. If it never appears because
    the device sits on a VLAN, guest, or IoT network that mDNS does not
    cross, add it by IP instead — step 10b below.
11. It appears there as *Discovered*, which is not the same as added. Add it
    to the ESPHome integration. Home Assistant normally takes the device's
    encryption key from the Device Builder add-on by itself and asks you
    for nothing; the key prompt is the fallback, not the normal case.
12. Start Add Integration → PVAutonomy again.
13. Select the second menu entry: "Adopt a running controller".
14. Enter the same model, site and number as before, then choose the
    running ESPHome device.
15. Finish adoption — PVAutonomy will use the existing device without reflashing.
```

> **Step 11 is not optional.** PVAutonomy adopts a device that Home Assistant
> already knows. A device that is only *discovered* has not been added yet, so
> the adoption wizard in step 13 will not find it.

> **Two different things are called "ESPHome".** The **ESPHome Device Builder**
> add-on (Settings → Add-ons) manages configuration files and compiles them —
> that is where your generated device YAML appears. The **ESPHome integration**
> (Settings → Devices & Services) lists devices that are already running on your
> network, so a freshly generated YAML will not show up there until you have
> flashed the device.

---

## Steps 8–11 in detail

The list above is the whole path. This section is what the four steps around
the flash actually look like on screen — the part where a first run tends to
stall.

### Before you flash: the browser decides

The Device Builder flashes over **Web Serial**, a browser API that only
Chromium-based browsers implement: Chrome, Edge, Chromium, Brave. In Safari or
Firefox the "Plug into this computer" option does not work, and the path simply
ends without telling you why.

There is a second condition that catches people on an ordinary Home Assistant
installation. Web Serial is available only on a **secure origin** — HTTPS, or
`localhost`. Home Assistant reached over plain HTTP at `http://<ip>:8123` is
neither. The Device Builder does not fail in that case; it sends you to the
third-party page **web.esphome.io**, with a message to this effect:

> This page is unable to flash your device over USB, because it is not being
> accessed over a secure (HTTPS) connection, or via localhost.

Nothing is broken, but you have just landed on an external site that no step
told you to expect. Either continue there — it is ESPHome's own hosted flasher
and it flashes the same file — or reach Home Assistant over HTTPS or
`localhost` and stay inside the Device Builder. Whichever you pick, it still
has to be a Chromium-based browser.

### 8. Open the Device Builder

Settings → Add-ons → **ESPHome Device Builder** → OPEN WEB UI. Your device is
already in the list: the wizard wrote the YAML into the directory the add-on
manages, so there is nothing to import, copy, or paste.

### 9. Validate first, then install

**Validate before you install.** On the device's card, open the three-dot menu
and choose **Validate**: it compiles the configuration without touching any
hardware, so a YAML problem shows up on its own instead of in the middle of a
flash, with the device connected and a serial port open.

If you remember one thing about this screen, remember the shape of it:
**⋮ → Validate, then Install.** Everything on the device's card hangs off that
same three-dot menu, and the two entries sit next to each other.

Then flash it:

1. Connect the Edge101 to this computer over USB.
2. Three-dot menu → **Install**.
3. In the ESPHome install dialog, choose the **first** option — the one that
   installs over the cable attached to this computer.
4. Pick the device's serial port in the browser dialog and let it run.

#### Two dialogs that are not ours

Steps 3 and 4 hand you to software we do not control, and neither dialog is
described anywhere in PVAutonomy. Expect them rather than the exact words:

- **The install dialog belongs to ESPHome, and it explains itself.** It opens
  by telling you that the first installation needs a USB cable and that later
  updates go over the network, then offers three ways to deliver the firmware:
  over the cable attached to *this* computer, over a cable attached to the Home
  Assistant server, or wirelessly. The first is the one you want; wireless is
  impossible before a device has firmware. The exact words are localized and
  change between versions, so pick by meaning, not by matching a string here.
- **The port dialog belongs to your browser.** Choosing the cable option makes
  the browser, not ESPHome, ask which serial device to talk to. It lists ports
  by chip or adapter name rather than by anything recognisable as an Edge101,
  and it will not let the page proceed until you pick one and confirm.

Both dialogs move and get reworded between versions, which is why they are
described here by what they do, not by the words on their buttons.

#### Finding the right port

If exactly one entry is offered, that is the device. If several are, the
reliable way to tell is to make the list change:

1. Open the port dialog with the Edge101 **unplugged** and note what is there —
   on a laptop these are usually Bluetooth ports and a debug console.
2. Cancel, plug the Edge101 in, and open the dialog again.
3. The entry that was not there before is the one you want.

What to look for: an entry whose name contains **usbserial**, **USB** or
**Serial** — on macOS something like `cu.usbserial-…` or "USB Single Serial",
often flagged as paired; on Windows a `COMx` port; on Linux `/dev/ttyUSB*`.
The device appears as its USB-to-serial bridge (a CP210x, CH340 or similar),
never under the name Edge101 or PVAutonomy.

What not to pick: a Bluetooth incoming port, or a debug console. Those are
present whether or not anything is plugged in, which is exactly why the
unplug/replug check settles it.

#### If no new port appears, suspect the cable

If the dialog offers **only** the entries that were already there with nothing
plugged in — a Bluetooth port and a debug console, no `usbserial` entry at all
— suspect the cable before anything else. A USB cable that carries power but no
data will charge the device and light it up while never presenting a port. This
is the single most common reason the list does not change, and it looks exactly
like a broken device.

Before assuming anything else, swap in a cable you know carries data — the one
that came with a phone and is used for file transfer is a safe bet — and repeat
the unplug/replug check. Cables shipped with power banks and chargers are the
usual culprits. If a known-good data cable still produces no new port, then it
is worth looking at drivers or the port itself.

**The first flash has to go over USB.** The wireless option installs over OTA,
and OTA needs firmware that is already running and already on your network —
which is exactly what a fresh device does not have. From the second flash
onwards, wireless works and the cable is no longer needed; see "Changing Wi-Fi
later" below for the one case that sends you back to USB.

The ESPHome CLI does the same two things: `esphome config <node_name>.yaml`
validates, `esphome run <node_name>.yaml` builds and installs, and the first
run there is a USB flash as well.

### 10. Wait for the device — and 10b when it never arrives

**10a — the normal case.** The device boots, joins your Wi-Fi, and Home
Assistant discovers it over mDNS. It appears under Settings → Devices &
Services → ESPHome as a *Discovered* device, usually within a minute or two of
the flash finishing.

**10b — the device is on a network mDNS does not cross.** A device on a VLAN,
a guest network, or a separate IoT subnet is typically reachable and still not
discovered: mDNS is link-local, and unless something on your network reflects
it across that boundary, the card does not appear. Add the device by hand
instead:

1. Settings → Devices & Services → **Add Integration**.
2. Choose **ESPHome**.
3. Enter the device's **IP address** — your router's client list or DHCP lease
   table will show it — and leave the port at **6053**.
4. Continue; from here it is the same as step 11.

This is a documented branch of the path, not a workaround. Where discovery
works, 10a is less typing. Where it cannot, 10b is how you get there.

### 11. Adding it, and the encryption key

*Discovered* is not the same as added, and step 11 is not optional (see the
note above). Open the discovered device and add it to the ESPHome integration.

**Normally you are not asked for a key.** Home Assistant reads the device's
encryption key from the ESPHome Device Builder add-on and adds the device
without prompting for anything. Do not wait for a dialog that is not coming.

**The prompt is the fallback.** Home Assistant asks for the key only when it
cannot obtain it from the add-on — the add-on is not installed, or it does not
hold this device's configuration, as in an add-on-less, CLI-only setup. What it
wants is the value of the `api_encryption_key_<node_name>` entry in your
ESPHome `secrets.yaml`. The wizard no longer prints that entry name on screen;
"Required ESPHome secrets" below spells out the naming rule, and the entry
itself is in the file.

---

## Example generated filename

Given the following wizard inputs:

| Input | Value |
|---|---|
| Model | Growatt SPH10K |
| Site | testsite |
| Number | 9 |

PVAutonomy generates:

| | |
|---|---|
| File path | `/config/esphome/sph10k-testsite-09.yaml` |
| ESPHome node name | `sph10k-testsite-09` |

The node name pattern is: `<model>-<site>-<number padded to 2 digits>`.

The exact filename depends on the model, site name, and device number you entered
in the wizard. Keep these values consistent between the initial YAML generation and
the later adoption step.

---

## Required ESPHome secrets

The generated YAML references four secrets and embeds no credential value of its
own. **You do not add them by hand.** Before it writes the device YAML, the
wizard checks your ESPHome `secrets.yaml` for all four, generates the two
credentials if they are missing, and asks you once for the Wi-Fi network if
that is missing. Anything already in the file is left exactly as it is — the
wizard appends, it never rewrites what is there.

Two of the four are bound to the device's node name, so a second self-built
device gets its own pair rather than sharing one key with the first. The
excerpt below shows which is which.

> **If you generated a device YAML with an earlier release,** regenerate it.
> Device configurations from earlier releases could contain a fallback access
> point whose password was the same in every generated file, together with a
> captive portal and a web server without authentication. The current
> generator produces none of the three.

**Generated YAML references (excerpt):**
```yaml
wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password

api:
  encryption:
    key: !secret api_encryption_key_sph10k-home-01

ota:
  - platform: esphome
    password: !secret ota_password_sph10k-home-01
```

The suffix is the node name from the model, site and device number you
entered — `sph10k-home-01` above. `wifi_ssid` and `wifi_password` stay
un-suffixed on purpose: they are the credentials of your network, not of one
device, and every device on it uses the same pair. It has to be a 2.4 GHz
network — the device has no 5 GHz radio.

**What ends up in your ESPHome `secrets.yaml`** (the wizard writes it):
```yaml
wifi_ssid: "your-wifi-network-name"
wifi_password: "your-wifi-password"
api_encryption_key_sph10k-home-01: "32-random-bytes-base64-encoded"
ota_password_sph10k-home-01: "32-hex-characters"
```

The API key is 32 random bytes from a CSPRNG, base64-encoded (44 characters),
and the OTA password is 16 random bytes as 32 hex characters. Both are
generated on your Home Assistant instance, and the wizard writes both to
your local `secrets.yaml`. Both are resolved at compile time and flashed onto the
device inside the firmware — that is what they are for: the API key
authenticates the encrypted connection between Home Assistant and the device,
and the OTA password authorises later wireless updates.

You can still maintain the file yourself if you prefer — write the entries
before running the wizard and it will use them untouched.

That is also the route for a network the Wi-Fi dialog will not accept. The
dialog checks what you type against the shape WPA2-Personal can carry, and
refuses anything outside it as a field error rather than trimming it into
shape or storing it in silence:

| Entry | What the dialog accepts |
|---|---|
| `wifi_ssid` | not blank, at most 32 characters, no control characters |
| `wifi_password` | not blank, 8 to 63 characters, printable ASCII only — letters, digits, spaces, ordinary punctuation |

Three cases therefore belong in `secrets.yaml` by hand, written before you run
the wizard:

- **An open network.** The dialog refuses an empty passphrase: an empty entry
  would be written, read back as present, and then fail the build with nothing
  to point at. It has no way to express "this network has no passphrase", so
  that entry is yours to write.
- **A passphrase outside printable ASCII.** Some routers accept one; a device
  cannot reliably join with it. ESPHome would compile it and the association
  would then fail on the air, with nothing to point at — so if you write one
  by hand, expect to debug the join rather than the build.
- **An SSID over 32 characters, or one carrying control characters.** A control
  character is almost always a paste artefact from a router's web UI rather
  than part of the real network name, so retype the entry before working
  around the check.

If the dialog refuses your passphrase, its error message points at this same
route; the SSID error only restates the rule.

> **Security:** Never paste real secrets, API keys, or OTA passwords into public
> GitHub issues, forums, or shared chat. Keep your `secrets.yaml` private.

---

## Wi-Fi / provisioning note

The device joins **your** network directly, the ordinary ESPHome way: the
generated YAML contains `wifi.ssid` and `wifi.password` as `!secret` references
resolved from your own `secrets.yaml` at compile time.

There is deliberately **no** fallback access point, **no** captive portal, and
**no** web server. A fallback AP would need a password baked into the firmware,
which for a shared base template means the same password on every device — a
universal default credential. A captive portal accepts Wi-Fi credentials from
anyone in radio range, and an unauthenticated web server on port 80 exposes
read and control access to anyone on the network. None of it is generated.

The practical consequence is worth reading before you change your network.

### Changing Wi-Fi later — plan it, and you will not need USB

Because there is no fallback AP, a device that cannot reach its configured
network is not reachable over the air. Do the change **while the device is
still on the old network**:

1. Edit `wifi_ssid` / `wifi_password` in your ESPHome `secrets.yaml`.
2. Recompile the device.
3. **Install over OTA while the device is still connected to the old network.**
   It accepts the update, reboots, and then looks for the *new* network.
4. **Only now** switch the router / SSID / password.

Done in that order the device comes up on the new network by itself, and no
cable is involved.

If the device is **already** offline — the network changed first, the password
was mistyped, the router was replaced — then OTA is gone and you reflash over
USB. That is the trade for not shipping a universal AP password, and it is a
deliberate choice.

> **Tip:** keep the device's `secrets.yaml` entries and its ESPHome config
> together, and change Wi-Fi during a maintenance window rather than at the
> same moment you swap the router.

### Optional: enable the web server, with authentication

The web server is off by default and is not generated. You may add it to your
own copy of the device YAML — but **do not add it without `auth:`**, which is
exactly how it was previously shipped and why it was removed:

```yaml
web_server:
  port: 80
  auth:
    username: !secret web_server_username
    password: !secret web_server_password
```

with, in your `secrets.yaml`:

```yaml
web_server_username: <choose-a-name>
web_server_password: <choose-a-strong-unique-password>
```

Pick a password unique to this device; do not reuse `ota_password`. Note that
`web_server` serves plain HTTP, so credentials and readings cross your LAN
unencrypted — enable it on a trusted network, or not at all. Home Assistant
itself needs none of this: it talks to the device over the encrypted ESPHome
API. The web server is a convenience for you, not a requirement.

---

## Entity-surface contract

PVAutonomy's adoption step binds to the running ESPHome device by MAC address and
then reads specific entities to monitor and control the inverter. The generated YAML
creates the exact entity surface that PVAutonomy expects.

**Do not rename or remove the following — adoption and monitoring depend on them:**

| Component | Why |
|---|---|
| `esphome.name` (node name) | Must match the wizard values used during YAML generation |
| `esphome.friendly_name`, and the `friendly_name` and `devicename` entries under `substitutions:` | The friendly name sets the prefix of every Home Assistant entity ID of this controller. The PVAutonomy dashboards address the entities by a prefix derived from the node name, so a changed name points them at entities that no longer exist |
| `modbus_controller.id: inverter` | Referenced by all generated sensor and control entities |
| UART `id` | Used by the Modbus controller |
| Modbus `id` | Used by the inverter controller |
| `number` with id `active_power_rate_device` | Core power control entity read by PVAutonomy |
| Sensor entity `id` fields | PVAutonomy matches sensors by id pattern — changing them breaks monitoring |
| Entity name suffixes `*_device` | PVAutonomy discovery uses these suffix patterns |
| Required diagnostic/status entities | Used by PVAutonomy health checks |

**Expected core ESPHome components in the generated YAML:**

| Component | Purpose |
|---|---|
| `esphome` | Device name, project, framework (ESP32/Arduino) |
| `api` + `!secret api_encryption_key_<node_name>` | HA integration authentication |
| `ota` + `!secret ota_password_<node_name>` | OTA firmware updates, authenticated with the device's OTA password |
| `uart` | RS485 serial interface to inverter |
| `modbus` | Modbus RTU protocol |
| `modbus_controller` (`id: inverter`) | Inverter register polling |
| `sensor` entities from registry | Inverter monitoring (power, SOC, etc.) |
| `number.active_power_rate_device` | Active power rate control (0–100%) |
| Diagnostic / status entities | Uptime, Wi-Fi signal, hardware family marker |

---

## What users may edit

**Generally safe:**
- Wi-Fi credentials — change them in your `secrets.yaml`, not in the device YAML
- `logger.level` (e.g. `INFO` for production use)
- Comments in the YAML
- `update_interval` values — safe within reason (very short intervals increase RS485 bus load)

**Deliberate trade-off — only if you accept the cost:**

- **Adding a `web_server:` with `auth:`** — this is a supported thing to do and
  the recipe is above, but it is not "generally safe", so it is not listed as
  such. What you are accepting: `web_server` speaks **plain HTTP**, and its
  `auth:` is HTTP Basic, so the UI password you set travels the LAN in
  effectively cleartext on every request, as do all inverter readings. The page
  also exposes **control**, not just display — anyone who reaches it with those
  credentials can operate the inverter's controls. Home Assistant does not need
  it: it talks to the device over the encrypted ESPHome API. Enable it only on a
  network you trust and only because you want the local UI. Never without
  `auth:`, which is exactly how it used to ship and why it was removed.

**Risky — avoid unless you know the implications:**

| What | Why it is risky |
|---|---|
| `esphome.name` (node name) | Changing it requires re-adoption and may orphan existing HA entities |
| Entity `id` fields | PVAutonomy entity discovery depends on these exact IDs |
| `*_device` entity name suffixes | PVAutonomy uses suffix patterns to locate entities |
| `modbus_controller.id` | All sensor/number entities reference this by ID |
| Modbus register addresses | Incorrect addresses read wrong registers or crash the controller |
| `value_type` / `register_type` | Wrong types produce invalid readings |
| `filters` / `multiply` on inverter sensors | Changes the reported values PVAutonomy uses |
| UART pin assignments | Only change if using different hardware with known pin mapping |
| Removing sensors | PVAutonomy may lose visibility into device state |

---

## What the generated configuration contains

The generated YAML:
- Contains **no credential value** — Wi-Fi, API and OTA credentials are
  `!secret` references resolved from your own `secrets.yaml`
- Exposes **no fallback AP, no captive portal and no web server**

### Secret names are bound to the device

The generator emits `api_encryption_key_<node_name>` and
`ota_password_<node_name>`, and the wizard provisions exactly those, so each
device has its own pair with no manual step.

Device YAMLs generated by earlier releases may still reference the generic
names `api_encryption_key` and `ota_password`, which every such device then
shares. Regenerate them, or rename the two references in the file to the
suffixed names and add the matching entries. Generic entries already in your
`secrets.yaml` are left alone — the wizard neither reuses nor removes them —
so an older device you have not rebuilt keeps working.

You own and manage your local ESPHome `secrets.yaml`. It is stored on your
Home Assistant instance.

> Do not paste real API keys, OTA passwords, or any `secrets.yaml` content
> into public GitHub issues, forums, or shared channels.

---

## Troubleshooting

**YAML file not found under `/config/esphome/`**
- Confirm the config-flow reached the "Ready to Flash" step (not an earlier error).
- Check HA logs for `pvautonomy_ops` errors during YAML generation.
- Try the wizard again: Add Integration → PVAutonomy → "Set up a new controller".

**Setup stops with "a file named ... already exists"**
- A device YAML with that node name is already in `/config/esphome/`.
  PVAutonomy never overwrites it — it may be a configuration you edited.
- Rename or remove the existing file in the ESPHome Device Builder, or run the
  wizard again with a different site name or device number.

**ESPHome compile fails**
- Confirm `secrets.yaml` contains all four entries: `wifi_ssid`,
  `wifi_password`, `api_encryption_key_<node_name>`, and
  `ota_password_<node_name>`. The wizard writes them; if one is absent, the
  setup was interrupted before it got that far.
- Check YAML indentation (use a YAML linter if needed).
- Check ESPHome version compatibility with the generated platform/framework versions.
- Review any manual edits to the generated YAML for syntax errors.

**Device does not appear in Home Assistant after flashing**
- If the device sits on a VLAN, guest, or IoT network that mDNS does not
  cross, it will typically not be discovered however long you wait. Add it by
  IP instead — step 10b above.
- Check Wi-Fi/LAN provisioning (see Wi-Fi / provisioning note above).
- Verify the `api_encryption_key_<node_name>` in `secrets.yaml` matches what
  was compiled.
- Check device power and network connectivity.
- **Expect nothing from the USB serial console.** The generated firmware sets
  `logger.baud_rate: 0` deliberately — logging on this path is API-only — so
  once the bootloader messages have scrolled past, the serial port stays
  silent. That silence is normal operation, not a hung device, and it is not
  a symptom worth debugging.
- Read the application logs over the network instead: the **Logs** button on
  the device's card in the ESPHome Device Builder, or `esphome logs
  <node_name>.yaml`. Both reach the device through the encrypted ESPHome API,
  so both need it to be on your network. If it never joined, there is no log
  to read and the Wi-Fi entries in `secrets.yaml` are the thing to check.
  There is no AP fallback to fall back to; see "Changing Wi-Fi later" above.

**Adoption step fails or does not find the device**
- Do not rename the ESPHome node name after flashing.
- Do not modify entity IDs in the YAML after flashing.
- Verify the device appears in HA under Settings → Devices & Services → ESPHome.
- Verify model/site/number in the adoption wizard match what was used during YAML
  generation.
- The adoption checks that the device offers the entities the generated
  firmware provides; its messages are explained in
  [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## What this guide does not cover

- Building or installing firmware from Home Assistant itself — this release
  does not do that
- Inverter models other than the Growatt SPH10K and MIC600
- Customising the dashboards PVAutonomy creates

---

## See also

- [INSTALLATION.md](INSTALLATION.md) — installing the integration
- [SETUP-WIZARD.md](SETUP-WIZARD.md) — the setup, screen by screen
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — messages you may see
- [FAQ.md](FAQ.md)
