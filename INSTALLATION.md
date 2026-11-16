# HORUS Dashboard Manager Lite v0.2.4 — no dashboard code to paste

**Scope:** A single existing storage-mode dashboard, `fuller-family-test`. Other dashboards, devices, automations, and Home Assistant settings are not deployment targets.

## Update the existing add-on

1. In Home Assistant, open Terminal & SSH and run `ha store reload` (only if the newest version is not visible).
2. Go to Settings → Apps → HORUS Dashboard Manager Lite, and update to **v0.2.4**.
3. Restart the add-on and open its Web UI. Test Home Assistant connection first.

## One-time private GitHub authorization

ChatGPT can update the existing **private** repository `hyhouse0001-lab/horus-home-os` through the connected GitHub app. Your Home Assistant device also needs permission to exchange dashboard snapshots and proposed revisions with that private repo.

1. In GitHub, go to **Settings → Developer settings → Personal access tokens → Fine-grained tokens** and generate a new token. Use the repository owner `hyhouse0001-lab`, repository access **Only select repositories**, select **horus-home-os**, and grant **Contents: Read and write**. No additional repository permissions are required.
2. **Do not send this token to ChatGPT or paste it into chat.**
3. In Home Assistant → Settings → Apps → HORUS Dashboard Manager Lite → **Configuration**, paste the token into the `github_token` password field. Save and restart the add-on.

This is a one-time credential setup, not copying or pasting YAML, JSON, or Python.

## Normal use

1. In the add-on Web UI, tap **Sync current dashboard to private GitHub**. The add-on uploads a full snapshot to `dashboard_sync/fuller-family-test.current.json` in the private repo.
2. Ask ChatGPT for dashboard design changes. It reads the snapshot and commits the full proposed configuration to `dashboard_sync/fuller-family-test.proposal.json` in the same private repo.
3. Open the add-on Web UI, tap **Preview proposed update**, examine the summary and exact JSON diff, and tap **Approve and apply this dashboard update**.
4. The add-on refuses stale versions, creates a private local backup, saves *only* `fuller-family-test`, verifies the result, and refreshes the private snapshot.
5. If necessary, use **Restore previous dashboard** in the add-on Web UI. It refuses to overwrite a dashboard changed by someone else after deployment.

The GitHub access token may need renewal when it expires. The code does not show or log the token. Never store private dashboard snapshots in the public add-on repository.

**Validation status:** Static Python compilation and automated tests pass in GitHub Actions. On-device GitHub authentication, write, verification, and rollback require a first controlled deployment test before the flow is considered production-verified. No write occurs without the owner's confirmation.

## HORUS Update Watcher (optional)
Install **HORUS Update Watcher** from the same repository, then enable Start on boot and Watchdog. It requires the Home Assistant Supervisor manager role, which is broad; its code has a hardcoded Dashboard Manager-only update target. GitHub is checked every 15 seconds. The app itself does not require a separate GitHub token.

## Heru and humidity controls
After updating HORUS Dashboard Manager to **v0.2.4**, open the manager and tap **Install Family Controls** once. This creates a dedicated `input_boolean.horus_heru_sleeping` helper and four presets:
`script.horus_heru_sleep`, `script.horus_heru_awake`, `script.horus_basement_humidity_up`, and `script.horus_basement_humidity_down`.
Sleep/awake taps change the Master Bedroom Spot to volume 0.1/0.7. The Samsung TV action is separate and asks for confirmation. No other scripts or automations are created.
