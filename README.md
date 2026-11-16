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
The Manager v0.2.4 has an explicit **Install Family Controls** button, which creates **one Heru sleeping helper and four fixed scripts** for the bedroom Alexa volume and basement dehumidifier setpoint. The button does not run device actions during installation. All family device interactions occur only when the user subsequently taps a dashboard button; the bedroom-TV turn-off requires a separate confirmation.
