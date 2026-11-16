# Install HORUS Dashboard Manager Lite v0.1.0

This is a **read-only diagnostic preview**, not the dashboard installer. It does not change any Home Assistant dashboard, device, or automation. It contains no private dashboard YAML.

1. Home Assistant → Settings → Add-ons → Add-on Store → ⋮ → Repositories.
2. Add `https://github.com/hyhouse0001-lab/horus-dashboard-manager-lite`.
3. Refresh the store if needed, select **HORUS Dashboard Manager Lite**, and install.
4. Start the add-on and open its web UI. The landing page returns diagnostic JSON.

**Not yet validated on Home Assistant Green hardware.** The add-on may fail to build or its Supervisor WebSocket read calls may return HTTP 503. If that happens, capture the add-on logs for diagnosis. Dashboard write and rollback functionality remain blocked pending security review.
