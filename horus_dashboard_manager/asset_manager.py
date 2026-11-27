"""Pinned, approval-bound dashboard files. No arbitrary URLs or shell execution."""
import base64
import hashlib
import json
import os
import re
import secrets
import tempfile
from pathlib import Path

ROOT = Path("/homeassistant/www/horus")
JOURNALS = Path("/data/horus_asset_backups")
LIMIT = 950000
TYPES = {".js": "module", ".css": "css"}
PASSIVE = {".woff", ".woff2", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".txt"}

class AssetError(Exception):
    pass

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

def relative(name):
    if not isinstance(name, str) or len(name) > 240 or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", p) for p in name.split("/")
    ) or Path(name).suffix not in TYPES.keys() | PASSIVE:
        raise AssetError("Invalid HORUS asset path")
    return name

def manifest(m):
    if not isinstance(m, dict) or set(m) != {"ref", "files"} or not isinstance(m["ref"], str) or not re.fullmatch(r"[a-f0-9]{40}", m["ref"]):
        raise AssetError("Asset manifest requires an immutable Git commit")
    if not isinstance(m["files"], list) or not 1 <= len(m["files"]) <= 64:
        raise AssetError("Invalid asset count")
    seen, total = set(), 0
    for i in m["files"]:
        if not isinstance(i, dict) or set(i) != {"path", "sha256", "size", "register"}:
            raise AssetError("Invalid asset entry")
        name = relative(i["path"])
        if name.casefold() in seen:
            raise AssetError("Duplicate asset path")
        seen.add(name.casefold())
        if type(i["size"]) is not int or not 1 <= i["size"] <= LIMIT:
            raise AssetError("Asset exceeds per-file limit")
        total += i["size"]
        if not isinstance(i["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", i["sha256"]):
            raise AssetError("Invalid asset checksum")
        if type(i["register"]) is not bool or i["register"] != (Path(name).suffix in TYPES):
            raise AssetError("JS/CSS registration must be explicit; passive assets cannot register")
    if total > 24000000:
        raise AssetError("Asset batch exceeds size limit")
    return m

def path(name):
    target = ROOT / relative(name)
    if not ROOT.parent.is_dir():
        raise AssetError("Home Assistant www mount unavailable")
    for p in [target, *target.parents]:
        if p.is_symlink():
            raise AssetError("Symlink in asset path")
    return target

def read(name):
    p = path(name)
    if not p.exists():
        return None
    if not p.is_file() or p.stat().st_size > LIMIT or p.stat().st_nlink != 1:
        raise AssetError("Existing asset is not a bounded regular file")
    return p.read_bytes()

def hash_or_none(raw):
    return None if raw is None else digest(raw)

def atomic(p, data, mode=0o644):
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".horus-", dir=p.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), mode)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        d = os.open(p.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(d)
        finally:
            os.close(d)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def resource_for(rows, name):
    if not isinstance(rows, list):
        raise AssetError("Invalid resource registry response")
    url = "/local/horus/" + relative(name)
    matches = [r for r in rows if isinstance(r, dict) and isinstance(r.get("url"), str) and r["url"].split("?", 1)[0] == url]
    if len(matches) > 1:
        raise AssetError("Duplicate resource registration")
    if not matches:
        return None
    r = matches[0]
    if not isinstance(r.get("id"), str) or not r["id"] or r.get("type") not in ("module", "css", "js"):
        raise AssetError("Resource not editable in storage mode")
    return {k: r[k] for k in ("id", "url", "type")}

def desired(i):
    return {"url": "/local/horus/" + i["path"] + "?v=" + i["sha256"][:16], "type": TYPES[Path(i["path"]).suffix]}

def same(a, b):
    return (a is None and b is None) or (a is not None and b is not None and a["url"] == b["url"] and a["type"] == b["type"])

def plan(m, resource):
    rows = resource("list") if any(i["register"] for i in manifest(m)["files"]) else []
    return [{"item": i, "before": hash_or_none(read(i["path"])),
             "old": resource_for(rows, i["path"]) if i["register"] else None,
             "new": desired(i) if i["register"] else None} for i in m["files"]]

def plan_hash(m, resource):
    return digest(canonical(plan(m, resource)))

def fetch_bytes(i, ref, fetch):
    r = fetch("home_assistant/www/horus/" + i["path"], ref=ref)
    if not isinstance(r, dict) or r.get("type") != "file" or r.get("encoding") != "base64":
        raise AssetError("GitHub did not return a regular asset file")
    v = r.get("content")
    if not isinstance(v, str) or len(v) > 1350000:
        raise AssetError("Asset response exceeds size limit")
    raw = base64.b64decode("".join(v.split()), validate=True)
    if len(raw) != i["size"] or digest(raw) != i["sha256"]:
        raise AssetError("Asset integrity mismatch: " + i["path"])
    return raw

def set_resource(resource, name, target):
    current = resource_for(resource("list"), name)
    if same(current, target):
        return
    if target is None:
        resource("delete", name=name, resource_id=current["id"])
    elif current is None:
        resource("create", name=name, url=target["url"], res_type=target["type"])
    else:
        resource("update", name=name, resource_id=current["id"], url=target["url"], res_type=target["type"])
    if not same(resource_for(resource("list"), name), target):
        raise AssetError("Resource registration did not verify")

def verify(m, resource):
    for e in plan(m, resource):
        if e["before"] != e["item"]["sha256"] or not same(e["old"], e["new"]):
            raise AssetError("Required asset not installed: " + e["item"]["path"])

def journal_path(name):
    if not isinstance(name, str) or not re.fullmatch(r"[a-f0-9]{32}", name):
        raise AssetError("Invalid asset backup ID")
    return JOURNALS / (name + ".json")

def load(name):
    return json.loads(journal_path(name).read_text())

def save(name, record):
    atomic(journal_path(name), canonical(record), 0o600)

def pending():
    return [p.stem for p in JOURNALS.glob("*.json") if load(p.stem)["status"] == "pending"]

def restore(name, resource):
    record = load(name)
    entries = record["entries"]
    rows = resource("list") if any(e["item"]["register"] for e in entries) else []
    # Preflight all conflicts and backup hashes before restoring anything.
    for e in entries:
        i = e["item"]
        if hash_or_none(read(i["path"])) not in (e["before"], i["sha256"]):
            raise AssetError("Asset changed after install: " + i["path"])
        if i["register"]:
            r = resource_for(rows, i["path"])
            if not (same(r, e["old"]) or same(r, e["new"])):
                raise AssetError("Resource changed after install: " + i["path"])
        backup = None if e["backup"] is None else base64.b64decode(e["backup"], validate=True)
        if hash_or_none(backup) != e["before"]:
            raise AssetError("Asset backup checksum mismatch")
    for e in reversed(entries):
        n = e["item"]["path"]
        if e["item"]["register"]:
            set_resource(resource, n, e["old"])
        if e["backup"] is None:
            path(n).unlink(missing_ok=True)
        else:
            atomic(path(n), base64.b64decode(e["backup"], validate=True))
        if hash_or_none(read(n)) != e["before"]:
            raise AssetError("Asset restore did not verify")
    record["status"] = "restored"
    save(name, record)

def install(m, expected, fetch, resource):
    if pending():
        raise AssetError("Unfinished asset transaction; recovery required")
    entries = plan(m, resource)
    if digest(canonical(entries)) != expected:
        raise AssetError("Asset state changed since preview")
    raw = [fetch_bytes(e["item"], m["ref"], fetch) for e in entries]
    if plan_hash(m, resource) != expected:
        raise AssetError("Asset state changed during download")
    for e in entries:
        prior = read(e["item"]["path"])
        if hash_or_none(prior) != e["before"]:
            raise AssetError("Asset state changed during backup")
        e["backup"] = None if prior is None else base64.b64encode(prior).decode("ascii")
    name = secrets.token_hex(16)
    save(name, {"status": "pending", "entries": entries})
    try:
        for e, data in zip(entries, raw):
            atomic(path(e["item"]["path"]), data)
        for e in entries:
            if e["item"]["register"]:
                set_resource(resource, e["item"]["path"], e["new"])
        verify(m, resource)
    except Exception:
        restore(name, resource)
        raise
    return name

def finish(name):
    record = load(name)
    record["status"] = "installed"
    save(name, record)
