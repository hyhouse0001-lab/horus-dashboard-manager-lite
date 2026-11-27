# HORUS DBM 0.3.0 — dashboard assets (candidate)

Owner-approved Apply may now include an optional `assets` object in the private dashboard proposal:
`{"ref":"<40-character immutable commit SHA>","files":[{"path":"horus-example.js","size":123,"sha256":"<64 lowercase hex>","register":true}]}`.
Sources are read only from home_assistant/www/horus/ at that exact private repository commit.
Destinations are under /homeassistant/www/horus (Home Assistant /config/www/horus).
JavaScript and CSS register as global Lovelace resources with a checksum-derived cache version; fonts, raster art and text do not register.
Per-file cap 950 KB, batch cap 24 MB, maximum 64 files. No remote shell, arbitrary URLs, server-side scripts, YAML, symlinks or writes to .storage.
The Home Assistant app mount grants configuration-folder write access at the container level; the implementation restricts its own file operations to www/horus. Global frontend modules are executable in the browser and must be reviewed before owner approval.
Preview shows exact commit, files, sizes, SHA-256 and global resource warning. Apply rechecks the dashboard base, proposed blob and asset baseline, verifies all downloads before writing, backs up replaced assets and resources, then saves the dashboard. Restore includes asset rollback. Interrupted operations fail closed and expose recovery.
No private dashboard data belongs in this public repository. Existing no-asset proposals remain compatible except changed views that introduce a missing HORUS custom module.
This is a candidate: real Home Assistant Supervisor mount, storage-mode resource commands, serving and device rendering must be smoke-tested before relying on it.
