"""Fixed, owner-approved installation of a pinned Nanit companion only."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

REF = "d2eea8c1b66f09efcb7dd51b929a1f2c1de58fa6"
SOURCE = "home_assistant/custom_components/horus_nanit_insights/"
FILES = {
    "manifest.json": ("9ebaf9f591af3cb6e7cef7c40049dd951f70954e", 288),
    "model.py": ("772b48a650a6502e267ae664af4dbb857c06d7a9", 1428),
    "__init__.py": ("7abe7512f7fb4408eebc4f55b94b08576fd11350", 3660),
    "configuration.fragment.yaml": ("6bdba12502907ca798f7490b171325d7015eb041", 40),
}
DOMAIN = "horus_nanit_insights"

def download(fetch):
    result = {}
    for name, (sha, size) in FILES.items():
        record = fetch(SOURCE + name, ref=REF)
        if not isinstance(record, dict) or record.get("encoding") != "base64" or record.get("sha") != sha:
            raise ValueError("Pinned companion file unavailable")
        raw = base64.b64decode(record["content"])
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if len(raw) != size or blob != sha:
            raise ValueError("Pinned companion integrity mismatch")
        result[name] = raw
    return result

def stage(fetch, root=Path("/homeassistant"), backup=Path("/data/nanit-insights-config.before")):
    root = Path(root)
    cfg = root / "configuration.yaml"
    parent = root / "custom_components"
    target = parent / DOMAIN
    if not root.is_dir() or cfg.is_symlink() or not cfg.is_file() or parent.is_symlink() or not parent.is_dir() or target.exists() or target.is_symlink():
        raise ValueError("Existing companion or unexpected configuration layout; no writes made")
    if backup.exists() or backup.is_symlink():
        raise ValueError("Previous installation backup exists; review before retry")
    original = cfg.read_bytes()
    if len(original) > 2_000_000:
        raise ValueError("Configuration size exceeds limit")
    if re.search(rb"(?m)^\s*['\"]?horus_nanit_insights['\"]?\s*:", original):
        raise ValueError("Companion configuration already present")
    files = download(fetch)
    fragment = files.pop("configuration.fragment.yaml")
    if not re.fullmatch(rb"horus_nanit_insights:\n  baby_name: [A-Za-z0-9 _-]+\n", fragment):
        raise ValueError("Unexpected fixed configuration fragment")
    manifest = json.loads(files["manifest.json"])
    if manifest.get("domain") != DOMAIN:
        raise ValueError("Unexpected companion domain")
    for name in ("model.py", "__init__.py"):
        compile(files[name], name, "exec")
    # No arbitrary paths or code supplied by web requests; immutable pinned files only.
    temp = Path(tempfile.mkdtemp(prefix=".horus-nanit-", dir=parent))
    config_temp = None
    created = False
    try:
        for name, raw in files.items():
            (temp / name).write_bytes(raw)
        temp.chmod(0o755)
        with backup.open("xb") as handle:
            handle.write(original)
        backup.chmod(0o600)
        fd, path = tempfile.mkstemp(prefix=".horus-nanit-config-", dir=root)
        config_temp = Path(path)
        with os.fdopen(fd, "wb") as handle:
            handle.write(original.rstrip(b"\n") + b"\n\n" + fragment)
        config_temp.chmod(cfg.stat().st_mode & 0o777)
        if cfg.read_bytes() != original or target.exists() or target.is_symlink():
            raise ValueError("Configuration changed during installation")
        temp.rename(target)
        created = True
        os.replace(config_temp, cfg)
        return "Files and configuration staged; Core check and approved restart still required."
    except Exception:
        if created and target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        raise
    finally:
        if temp.exists():
            shutil.rmtree(temp)
        if config_temp is not None and config_temp.exists():
            config_temp.unlink()
