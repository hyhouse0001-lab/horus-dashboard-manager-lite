# HORUS Dashboard Manager Lite — diagnostic preview v0.1.0

**READ-ONLY.** This installable Home Assistant add-on lets an administrator test Supervisor ingress and read access to the storage-mode `fuller-family-test` dashboard. It cannot install or change dashboards. No private household dashboard YAML is included.

## Install
Add repository URL `https://github.com/hyhouse0001-lab/horus-dashboard-manager-lite` in Home Assistant Add-on Store → Repositories. Install HORUS Dashboard Manager Lite, start it, then open its ingress panel.

## Diagnostic endpoints
- `/` or `/health`: add-on status
- `/dashboards`: dashboard inventory
- `/dashboard/fuller-family-test`: read test dashboard

The initial ingress page is JSON, not a graphical installer. The Supervisor WebSocket authentication path and ingress source IP have **not** been validated on actual Home Assistant Green hardware. If dashboard reads return 503, report the error; do not attempt dashboard writes. This preview intentionally does not contain `lite.py`, any write command, or rollback.
