# HORUS Update Watcher v0.1.1

Separate Home Assistant add-on for **Horus DBM**. It polls the public release manifest every 15 seconds. Before refreshing, it requires successful CI for the exact current main commit and checks that commit's pinned manifest version.

It verifies the installed DBM identity (current or legacy name, exact slug suffix, repository URL), and uses only Supervisor GET add-on discovery and POST /store/reload. It verifies that the store advertises the expected version. It never installs an update, modifies dashboards, runs shell commands or restarts add-ons. If already available, it does not reload the store again.

## One-time setup

In Home Assistant, Settings → Apps → App Store, refresh the store if necessary, install HORUS Update Watcher v0.1.1 from the existing HORUS add-on repository, and start it. Enable Start on boot and Watchdog. The add-on requires Supervisor manager authorization for store reload; its implementation narrows the permitted API paths. Leave DBM Auto Update disabled if you want manual installation. Check watcher logs and the DBM update entity to verify on-device operation.

The watcher does not bootstrap its own installation. No on-device verification is implied by passing CI.
