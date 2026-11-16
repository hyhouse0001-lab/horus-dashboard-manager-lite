# HORUS Dashboard Manager Lite v0.2.4

**One dashboard, no copy/paste.** This Home Assistant add-on reads and edits only `fuller-family-test`, using the Home Assistant Core WebSocket API with Supervisor authentication.

## Safeguards
- The Home Assistant WebSocket command allowlist contains only `lovelace/config` and `lovelace/config/save` for the exact `fuller-family-test` URL path.
- The add-on is reachable via Home Assistant admin-only ingress; no host ports are published.
- Live dashboard configuration and changes are synchronized through the **private** `hyhouse0001-lab/horus-home-os` GitHub repository.
- GitHub credential is stored only in the add-on's password-type configuration and read from `/data/options.json`. Do not disclose credentials.
- Every proposed change requires an owner click in the add-on Web UI. There is no automatic remote push/deploy endpoint.
- The live configuration hash must match the original synchronized snapshot before an apply is allowed.
- A copy of the previous config is saved on the add-on's persistent `/data` volume before writing. Save is verified and rollback is available.
- The editor blocks new direct `lock.unlock` and `alarm_control_panel.alarm_disarm` card shortcuts until separately reviewed.
- This add-on does not execute GitHub code, shell scripts, or Home Assistant device actions.

For setup, see [INSTALLATION.md](../INSTALLATION.md). The public repository contains source code only, not the household dashboard.

## v0.2.4 narrow household exception
Although Lovelace synchronization remains restricted to `fuller-family-test`, clicking **Install Family Controls** explicitly opts in to create one named Home Assistant toggle helper and four pre-defined scripts. The operation accepts no arbitrary script bodies, entity IDs or external commands from the browser or GitHub. It is not performed automatically by applying a dashboard proposal. It requires a separate approval click.

The 15-second updater is an independent add-on with its own Supervisor manager permissions, not included in the Dashboard Manager runtime.
