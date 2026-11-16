# HORUS Dashboard Manager Lite — installation status

This is the **public distribution repository**, separate from the private HORUS HOME OS engineering repository.

**Not installable yet:** the add-on runtime is held back pending security review and hardware validation. Do not attempt to install from this repository until a signed-off release is published.

## Intended installation
1. In Home Assistant: Settings → Add-ons → Add-on Store → menu → Repositories.
2. Add `https://github.com/hyhouse0001-lab/horus-dashboard-manager-lite`.
3. After a reviewed release adds the add-on package, reload the store and install HORUS Dashboard Manager Lite.
4. Confirm the existing storage-mode `fuller-family-test` dashboard and create a backup before the first update.

The installer must not include any household YAML, entity IDs, credentials, or backups. Deployment to Home Assistant Green has **not** occurred.
