# Installation

## Prerequisites

- Home Assistant **2024.1.0** or newer
- [HACS](https://hacs.xyz/) installed and running

## Install via HACS

1. Open **HACS** in the Home Assistant sidebar.
2. Go to **Integrations**.
3. Click the three-dot menu (top right) and select **Custom repositories**.
4. Enter:
   - **Repository:** `PVAutonomy/pvautonomy-ops`
   - **Category:** Integration
5. Click **Add**.
6. Find **PVAutonomy** in the integration list and click **Download**.
7. **Restart Home Assistant.**

## Add the Integration

1. Go to **Settings > Devices & Services > Add Integration**.
2. Search for **PVAutonomy**.
3. Enter a name (default: "PVAutonomy") and poll interval (default: 60 seconds).
4. Click **Submit**.

The integration is now active. Continue with [Setup](SETUP-WIZARD.md) to configure the build backend.

## Update

HACS will notify you when a new version is available. Click **Update** in HACS and restart Home Assistant.

## Uninstall

1. Go to **Settings > Devices & Services**.
2. Find **PVAutonomy** and click the three-dot menu > **Delete**.
3. Open HACS > Integrations, find PVAutonomy, and click **Remove**.
4. Restart Home Assistant.

---

# Installation (Deutsch)

## Voraussetzungen

- Home Assistant **2024.1.0** oder neuer
- [HACS](https://hacs.xyz/) installiert und aktiv

## Installation ueber HACS

1. **HACS** in der Home Assistant Seitenleiste oeffnen.
2. Zu **Integrationen** navigieren.
3. Drei-Punkte-Menue (oben rechts) > **Benutzerdefinierte Repositories**.
4. Eingeben:
   - **Repository:** `PVAutonomy/pvautonomy-ops`
   - **Kategorie:** Integration
5. **Hinzufuegen** klicken.
6. **PVAutonomy** in der Liste finden und **Herunterladen** klicken.
7. **Home Assistant neu starten.**

## Integration hinzufuegen

1. **Einstellungen > Geraete & Dienste > Integration hinzufuegen**.
2. Nach **PVAutonomy** suchen.
3. Name eingeben (Standard: "PVAutonomy") und Abfrageintervall (Standard: 60 Sekunden).
4. **Absenden** klicken.

Die Integration ist jetzt aktiv. Weiter mit [Einrichtung](SETUP-WIZARD.md) fuer die Build-Backend-Konfiguration.
