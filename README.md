# HORUS Dashboard Manager Lite

A narrowly scoped Home Assistant add-on for **Fuller Family Test** only. Version **0.2.4** supports authenticated live read, private GitHub snapshot sync, reviewable proposed changes, user-approved Lovelace writes, local backup, verification, and rollback.

**No dashboard code copy/paste required.** The user does a one-time, narrowly scoped GitHub token setup in the add-on configuration and explicitly approves deployments in the Web UI.

**Status:** CI syntax and unit checks pass. Home Assistant Green read access has been verified; write and rollback require on-device validation.

[Installation / setup](INSTALLATION.md) · [Add-on details](horus_dashboard_manager/README.md)

## Visual interface
The admin-only Web UI uses a matching dark neon layout with consistent action buttons for Sync, Preview, Connection Test, Apply, Cancel, and Rollback. No JavaScript or public dashboard configuration is shipped.

## Optional 15-second updater
The same repository includes **HORUS Update Watcher**, a separate add-on requiring a one-time installation and Supervisor manager-level authorization. It checks the public GitHub release every 15 seconds, waits for passing CI, then refreshes the store and auto-updates **only HORUS Dashboard Manager Lite**. It does not manage other apps or alter dashboards. Automatic updates are **not active** until installed and started on Home Assistant Green.

## Owner-approved family controls
The Manager v0.2.9 has an explicit **Install Family Controls** button, which creates **one Heru sleeping helper and four fixed scripts** for the bedroom Alexa volume and basement dehumidifier setpoint. The button does not run device actions during installation. All family device interactions occur only when the user subsequently taps a dashboard button; the bedroom-TV turn-off requires a separate confirmation.

## Frigidaire & Nest Controls (v0.2.9)
When the selected dashboard proposal references a fixed HORUS helper script that isn't installed yet, **Preview** displays an **Install Required Controls** button instead of an unhelpful error. The installation step creates the required scripts, then provides a button to preview again. No physical AC, thermostat, lock, or alarm action occurs during setup.

The immutable device allowlist covers Nest Learning Thermostat 4th generation and five Frigidaire room AC entities: living room, primary bedroom, dining room, Heru's room, and guest room. Ten fixed AC ±1°F scripts clamp target setpoints to 60–90°F. The Lovelace card uses `climate.set_hvac_mode` with **cool** / **off** for each AC, and only exposes current temperature and adjustment buttons while powered on. The independent dashboard proposal is stored only in the private repository.
