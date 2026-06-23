# Installation

## Prerequisites

- A supported Home Assistant version. The package metadata sets the minimum
  Home Assistant version; treat that as the supported floor. The current
  release (`pvautonomy_ops` 0.4.16) was validated on Home Assistant Core
  2026.6.x — that is the **validated test environment**, not necessarily a new
  hard minimum.
- For normal customers: the **PVAutonomy Installer/Updater add-on** (no HACS
  required). For developers/power-users: **HACS**.

## Two supported installation paths

Both paths install the **same** `pvautonomy_ops` 0.4.16 release artifact (same
version, same SHA). Pick one:

### 1. Customer / app path — PVAutonomy Installer/Updater add-on

The supported path for normal customers.

1. Install/enable the **PVAutonomy Installer/Updater add-on**.
2. Set the channel to **`stable`**.
3. The add-on installs `pvautonomy_ops` 0.4.16 into
   `/config/custom_components/pvautonomy_ops/`.
4. **Restart Home Assistant.**

HACS is **not** required for this path.

### 2. Developer / HACS path

Suitable for developers and power-users.

1. Open **HACS** in the Home Assistant sidebar.
2. Go to **Integrations**.
3. Three-dot menu (top right) → **Custom repositories**.
4. Enter:
   - **Repository:** `PVAutonomy/pvautonomy-ops`
   - **Category:** Integration
5. Click **Add**.
6. Find **PVAutonomy** in the list and click **Download** (stable, 0.4.16).
7. **Restart Home Assistant.**

## What you do NOT need

- **No firmware definitions under `/config`.** Firmware definitions ship
  **bundled in the integration release** under
  `custom_components/pvautonomy_ops/data/firmware_defs/`. You do not need any
  files under `/config/inverter-registry` or `/config/esphome` — those are no
  longer customer/product distribution paths.
- **No pip dependency resolution at install time** (`manifest.json` declares
  `requirements = []`; `pyhpke` is vendored in-tree).

> **Managed Build Service access is separate.** Installing the integration
> (via either path) does **not** by itself grant access to the Managed Build
> Service. Triggering firmware builds requires a PVAutonomy Managed Build
> Service API key (shown as `pva_...`), provisioned by your PVAutonomy
> provider. See [Setup](SETUP-WIZARD.md) and [Security](SECURITY.md).

## Add the Integration

1. Go to **Settings > Devices & Services > Add Integration**.
2. Search for **PVAutonomy**.
3. Enter a name (default: "PVAutonomy") and poll interval (default: 60 seconds).
4. Click **Submit**.

The integration is now active. Continue with [Setup](SETUP-WIZARD.md) to
configure the build backend.

## Update

- **Installer/Updater add-on (stable):** updates `pvautonomy_ops` to the
  current stable release; restart Home Assistant afterward.
- **HACS:** HACS notifies you when a new version is available. Click **Update**
  in HACS and restart Home Assistant.

## Uninstall

1. Go to **Settings > Devices & Services**.
2. Find **PVAutonomy** and click the three-dot menu > **Delete**.
3. Remove the integration files via the path you installed with (HACS >
   Integrations > Remove, or the Installer/Updater add-on).
4. Restart Home Assistant.

---

# Installation (Deutsch)

## Voraussetzungen

- Eine unterstützte Home-Assistant-Version. Die Mindestversion steht in den
  Paket-Metadaten und gilt als unterstützte Untergrenze. Das aktuelle Release
  (`pvautonomy_ops` 0.4.16) wurde mit Home Assistant Core 2026.6.x validiert —
  das ist die **validierte Testumgebung**, nicht zwingend eine neue
  Mindestanforderung.
- Für normale Kunden: das **PVAutonomy Installer/Updater Add-on** (kein HACS
  nötig). Für Entwickler/Power-User: **HACS**.

## Zwei unterstützte Installationspfade

Beide Pfade installieren dasselbe `pvautonomy_ops` 0.4.16 Release-Artefakt
(gleiche Version, gleicher SHA). Einen wählen:

### 1. Kunden-/App-Pfad — PVAutonomy Installer/Updater Add-on

Der unterstützte Pfad für normale Kunden.

1. **PVAutonomy Installer/Updater Add-on** installieren/aktivieren.
2. Channel auf **`stable`** setzen.
3. Das Add-on installiert `pvautonomy_ops` 0.4.16 nach
   `/config/custom_components/pvautonomy_ops/`.
4. **Home Assistant neu starten.**

HACS ist für diesen Pfad **nicht** erforderlich.

### 2. Entwickler-/HACS-Pfad

Geeignet für Entwickler und Power-User.

1. **HACS** in der Seitenleiste öffnen.
2. Zu **Integrationen** navigieren.
3. Drei-Punkte-Menü (oben rechts) → **Benutzerdefinierte Repositories**.
4. Eingeben:
   - **Repository:** `PVAutonomy/pvautonomy-ops`
   - **Kategorie:** Integration
5. **Hinzufügen** klicken.
6. **PVAutonomy** in der Liste finden und **Herunterladen** (stable, 0.4.16).
7. **Home Assistant neu starten.**

## Was NICHT nötig ist

- **Keine Firmware-Definitionen unter `/config`.** Firmware-Definitionen werden
  **gebündelt im Integration-Release** unter
  `custom_components/pvautonomy_ops/data/firmware_defs/` ausgeliefert. Es werden
  keine Dateien unter `/config/inverter-registry` oder `/config/esphome`
  benötigt — diese sind keine Kunden-/Produkt-Distributionspfade mehr.
- **Keine pip-Abhängigkeitsauflösung zur Installationszeit** (`manifest.json`
  `requirements = []`; `pyhpke` ist in-tree vendored).

> **Managed-Build-Service-Zugang ist separat.** Die Installation der Integration
> (über einen der beiden Pfade) gewährt **nicht** automatisch Zugang zum
> Managed Build Service. Firmware-Builds benötigen einen PVAutonomy
> Managed-Build-Service-API-Key (als `pva_...` dargestellt), der vom
> PVAutonomy-Anbieter bereitgestellt wird. Siehe [Einrichtung](SETUP-WIZARD.md)
> und [Security](SECURITY.md).

## Integration hinzufügen

1. **Einstellungen > Geräte & Dienste > Integration hinzufügen**.
2. Nach **PVAutonomy** suchen.
3. Name eingeben (Standard: "PVAutonomy") und Abfrageintervall (Standard: 60
   Sekunden).
4. **Absenden** klicken.

Die Integration ist jetzt aktiv. Weiter mit [Einrichtung](SETUP-WIZARD.md) für
die Build-Backend-Konfiguration.
