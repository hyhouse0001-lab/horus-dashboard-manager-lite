# HORUS Update Watcher

A **separate** Home Assistant Green add-on; installs once from the same custom add-on repository as HORUS Dashboard Manager Lite.

- Checks the public GitHub configuration every **15 seconds**, bypassing default raw-file CDN caching with unique 15-second bucket URLs.
- Reads the latest **Dashboard Manager Safety Checks** GitHub Actions result before attempting any installation.
- Calls Supervisor store reload **only when a verified newer version is published**.
- Finds exactly one installed add-on named **HORUS Dashboard Manager Lite** and verifies its configured GitHub repository URL.
- Updates only that exact app through the Supervisor `/store/addons/<slug>/update` API, requesting a pre-update backup.
- Never modifies Home Assistant dashboards or devices; never logs or stores credentials; no configuration for arbitrary targets.
- Failure cooldown: 10 minutes before retrying an update after a network/Supervisor error. GitHub polling remains every 15 seconds.
- Requires `hassio_api: true` with Supervisor `manager` role, which is a relatively broad permission. This add-on's implementation restricts itself to fixed, audited endpoints. **Install only if you accept that privilege.**
- Built-in add-on Auto Update may remain enabled as a fallback, but is not necessary for HORUS Manager once this watcher is verified.

## Installation
1. In HA go to **Settings → Apps → App Store**. Run one final `ha store reload` in Terminal & SSH if this new add-on does not appear.
2. Install **HORUS Update Watcher** from the existing HORUS add-on repository.
3. Enable **Start on boot**, **Watchdog**, and start the add-on.
4. Open the **Logs** tab and verify the first **GitHub published version** and the targeted installed app were detected.
5. To stop it temporarily, stop this add-on; HORUS Dashboard Manager continues working.

**On-device status:** Not yet installed or integration-tested. GitHub CI tests validate the watcher only in isolation. Do not claim automatic updates are active until it is running on Home Assistant.
