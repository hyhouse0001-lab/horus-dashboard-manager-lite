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


## Required agent release and installation workflow (owner-approved)

When the owner requests a HORUS DBM enhancement or upgrade, the implementing agent owns the release through installation and verification, not merely a PR or instructions. This standing authorization is limited to HORUS DBM itself; dashboard proposals still require the owner's explicit Approve & Apply.

1. Inspect the installed version and repository main; implement on a branch, bump the manifest and corresponding version assertions, and add relevant regression tests. Open a PR and require passing safety checks on its exact head. Merge only the validated head. Never bypass failing checks.
2. Confirm the intended version on main and its release checks. Read Home Assistant entity `update.horus_dashboard_manager_lite_update` using render_template; inspect installed_version, latest_version, in_progress and state.
3. If the store has not discovered the release, request `homeassistant.update_entity` targeting that entity, then re-read. This refreshes the entity, not necessarily the Supervisor store. The existing optional watcher may refresh the store if installed and running; verify rather than assume. Recheck availability before declaring a blocker. Use a supported store-refresh action if exposed; never invent one or claim to click UI remotely.
4. When latest_version equals the intended validated release, state is on, and no installation is running, call `update.install` with target entity_id `update.horus_dashboard_manager_lite_update` and no version argument (specific-version installation is unsupported). Do not install an unexpected version or update unrelated entities.
5. Re-read the entity until installed_version equals the intended version, latest_version matches, and in_progress is false. Verify manager availability where possible. Report actual evidence, any remaining functional test, and blockers; do not claim success from an action acknowledgement alone.
6. For v0.3.2 and later, successful Approve & Apply requests cache clear and start-URL reload of the fixed entryway Fully Kiosk browser. Never reboot the tablet or refresh on failed apply. Dashboard approval remains mandatory.

Verified precedent: v0.3.2, PR #7, merge 425a465e152b914b1ec2f9992991ad1bb13dce22; Home Assistant reported installed=0.3.2, latest=0.3.2, in_progress=False after update.install. Physical post-apply refresh remained to be observed.
