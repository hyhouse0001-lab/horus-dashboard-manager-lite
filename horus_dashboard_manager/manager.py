"""HORUS Dashboard Manager: single-dashboard, human-approved GitHub bridge.

Allowed target: storage-mode Lovelace dashboard fuller-family-test ONLY.
GitHub repository is private. No tokens are returned to HTTP clients or logs.
Requires an administrator to click Apply; no remote command execution.
"""
import base64
import datetime
import difflib
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlsplit
import re
from urllib.request import Request, urlopen

PATH = "fuller-family-test"
PRIVATE_REPO = "hyhouse0001-lab/horus-home-os"
BRANCH = "main"
SNAPSHOT_PATH = "dashboard_sync/fuller-family-test.current.json"
PROPOSAL_PATH = "dashboard_sync/fuller-family-test.proposal.json"
BACKUPS_DIR = "/data/horus_dashboard_backups"
STATE_PATH = "/data/horus_dashboard_state.json"
MAX_CONFIG_BYTES = 850_000
MAX_RESPONSE = 1_200_000
CSRF = secrets.token_urlsafe(32)
LOCK = threading.RLock()
LOG = logging.getLogger("horus-dashboard-manager")


class BridgeError(Exception):
    """Safe, short error suitable for an authenticated ingress page."""


class MissingControls(BridgeError):
    """Owner must approve installation of the required fixed device scripts."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def check_config(config, restoring=False):
    if not isinstance(config, dict) or not isinstance(config.get("views"), list) or not config["views"]:
        raise BridgeError("Configuration must contain at least one dashboard view")
    if len(canonical(config).encode("utf-8")) > MAX_CONFIG_BYTES:
        raise BridgeError("Dashboard configuration is too large")
    # Narrow exception for this exact front-door lock, with explicit confirmation.
    # Never permit an unconfirmed unlock or any alarm disarm shortcut.
    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if not restoring and key in ("service", "perform_action") and isinstance(value, str):
                    if value in ("alarm_control_panel.alarm_disarm",
                                 "alarm_control_panel.alarm_arm_home",
                                 "alarm_control_panel.alarm_arm_away"):
                        allowed_ring = (
                            key == "perform_action"
                            and node.get("action") == "perform-action"
                            and node.get("target") == {"entity_id": "alarm_control_panel.home_alarm"}
                            and node.get("data") in (None, {})
                        )
                        if value == "alarm_control_panel.alarm_disarm":
                            allowed_ring = allowed_ring and (
                                isinstance(node.get("confirmation"), dict)
                                and bool(str(node["confirmation"].get("text", "")).strip())
                            )
                        if not allowed_ring:
                            raise BridgeError("Ring alarm action requires the exact MQTT alarm entity; disarm requires confirmation")
                    if value == "lock.unlock":
                        allowed = (
                            key == "perform_action"
                            and node.get("action") == "perform-action"
                            and node.get("target") == {"entity_id": "lock.front_of_house_front_door"}
                            and isinstance(node.get("confirmation"), dict)
                            and bool(str(node["confirmation"].get("text", "")).strip())
                            and node.get("data") in (None, {})
                        )
                        if not allowed:
                            raise BridgeError("Unlock requires confirmation and the exact front-door entity")
                    if value == "climate.set_hvac_mode":
                        entity_id = node.get("target", {}).get("entity_id")
                        allowed_hvac = (
                            {"heat", "off"} if entity_id == NEST else
                            {"cool", "off"} if entity_id in AC_UNITS.values() else set()
                        )
                        if not (
                            key == "perform_action"
                            and node.get("action") == "perform-action"
                            and node.get("data") in (
                                {"hvac_mode": mode} for mode in allowed_hvac
                            )
                        ):
                            raise BridgeError("HVAC on/off action is not allowed for this device or mode")
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(config)
    return config


def ha_call(command, *, restore_original=False, **args):
    if command not in ("lovelace/config", "lovelace/config/save"):
        raise BridgeError("Unsupported Home Assistant operation")
    if args.get("url_path") != PATH:
        raise BridgeError("Only Fuller Family Test may be accessed")
    if command == "lovelace/config" and set(args) != {"url_path"}:
        raise BridgeError("Invalid dashboard read request")
    if command == "lovelace/config/save" and set(args) != {"url_path", "config"}:
        raise BridgeError("Invalid dashboard save request")
    if command == "lovelace/config/save":
        check_config(args["config"], restoring=restore_original)
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise BridgeError("Supervisor token unavailable")
    from websockets.sync.client import connect
    with connect("ws://supervisor/core/websocket",
                 open_timeout=8, close_timeout=3, max_size=MAX_RESPONSE) as ws:
        greeting = json.loads(ws.recv(timeout=8))
        if greeting.get("type") != "auth_required":
            raise BridgeError("Unexpected authentication challenge")
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(ws.recv(timeout=8)).get("type") != "auth_ok":
            raise BridgeError("Home Assistant authentication rejected")
        ws.send(json.dumps({"id": 1, "type": command, **args}))
        response = json.loads(ws.recv(timeout=20))
        if response.get("type") != "result" or response.get("id") != 1 or not response.get("success"):
            raise BridgeError("Home Assistant rejected the dashboard operation")
        return response.get("result")


def read_dashboard():
    result = ha_call("lovelace/config", url_path=PATH)
    if not isinstance(result, dict) or not isinstance(result.get("views"), list):
        raise BridgeError("Home Assistant returned an unexpected dashboard format")
    return result


def save_dashboard(config, restoring=False):
    ha_call("lovelace/config/save", restore_original=restoring,
            url_path=PATH, config=check_config(config, restoring=restoring))


def github_token():
    try:
        with open("/data/options.json", encoding="utf-8") as handle:
            value = json.load(handle).get("github_token", "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    except (OSError, ValueError):
        pass
    raise BridgeError("Configure a GitHub token in the add-on Configuration tab first")


def github_request(path, method="GET", payload=None):
    token = github_token()
    api = "https://api.github.com/repos/" + PRIVATE_REPO + "/contents/" + quote(path, safe="/")
    if method == "GET":
        api += "?ref=" + BRANCH
    body = None
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = Request(api, data=body, method=method, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "HORUS-Dashboard-Manager/0.2",
        "Content-Type": "application/json",
    })
    try:
        with urlopen(req, timeout=15) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code == 404 and method == "GET":
            return None
        raise BridgeError("GitHub request failed (HTTP " + str(exc.code) + ")") from None
    except (URLError, TimeoutError):
        raise BridgeError("GitHub is unreachable from this add-on") from None


def github_get(path):
    record = github_request(path)
    if record is None:
        return None, None
    if not isinstance(record, dict) or record.get("encoding") != "base64":
        raise BridgeError("Unexpected GitHub file response")
    try:
        raw = base64.b64decode(record["content"], validate=False)
        if len(raw) > MAX_RESPONSE:
            raise BridgeError("GitHub file exceeds size limit")
        return json.loads(raw.decode("utf-8")), record["sha"]
    except (KeyError, ValueError, UnicodeDecodeError):
        raise BridgeError("GitHub file is not valid JSON") from None


def github_put(path, document, message):
    current, sha = github_get(path)
    _ = current
    payload = {
        "message": message,
        "branch": BRANCH,
        "content": base64.b64encode(
            (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        ).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    return github_request(path, "PUT", payload)


def snapshot_document(config):
    return {
        "schema_version": 1,
        "dashboard": PATH,
        "base_sha256": digest(config),
        "synced_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "config": config,
    }


def sync_to_github():
    live = read_dashboard()
    github_put(SNAPSHOT_PATH, snapshot_document(live),
               "Sync live Fuller Family Test dashboard for HORUS review")
    return {"views": len(live["views"]), "hash": digest(live)}


def load_proposal():
    document, sha = github_get(PROPOSAL_PATH)
    if document is None:
        raise BridgeError("No proposal in the private GitHub repository yet")
    if not isinstance(document, dict):
        raise BridgeError("Invalid proposal structure")
    if document.get("schema_version") != 1 or document.get("dashboard") != PATH:
        raise BridgeError("Proposal targets the wrong dashboard or version")
    base = document.get("base_sha256", "")
    summary = document.get("summary", "")
    if not isinstance(base, str) or len(base) != 64 or any(ch not in "0123456789abcdef" for ch in base):
        raise BridgeError("Proposal has an invalid base hash")
    if not isinstance(summary, str) or not (1 <= len(summary) <= 500):
        raise BridgeError("Proposal requires a short change summary")
    check_config(document.get("config"))
    validate_required_controls(document["config"])
    return document, sha


def require_fresh(proposal, live):
    if not hmac.compare_digest(proposal["base_sha256"], digest(live)):
        raise BridgeError("Dashboard changed since sync. Sync again before applying.")
    if digest(proposal["config"]) == digest(live):
        raise BridgeError("Proposal is identical to the current dashboard")


def backup(config):
    os.makedirs(BACKUPS_DIR, mode=0o700, exist_ok=True)
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    name = timestamp + "-" + digest(config)[:12] + ".json"
    file_path = os.path.join(BACKUPS_DIR, name)
    encoded = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd = os.open(file_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    return name


def state_read():
    try:
        with open(STATE_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def state_write(info):
    os.makedirs(os.path.dirname(STATE_PATH), mode=0o700, exist_ok=True)
    temporary = STATE_PATH + ".tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(info, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, STATE_PATH)


def apply_proposal(requested_sha):
    proposal, sha = load_proposal()
    if not isinstance(requested_sha, str) or not hmac.compare_digest(sha, requested_sha):
        raise BridgeError("Proposal changed since preview. Review the new version first.")
    before = read_dashboard()
    require_fresh(proposal, before)
    prior_hash = digest(before)
    name = backup(before)
    target = proposal["config"]
    try:
        save_dashboard(target)
        actual = read_dashboard()
        if digest(actual) != digest(target):
            raise BridgeError("Saved dashboard did not match the proposed version")
    except Exception:
        # Best-effort repair to the verified pre-apply configuration.
        try:
            save_dashboard(before, restoring=True)
            if digest(read_dashboard()) != prior_hash:
                LOG.error("Rollback verification failed; check dashboard manually")
        except Exception:
            LOG.exception("Automatic dashboard rollback failed")
        raise
    state_write({
        "backup": name,
        "before_sha256": prior_hash,
        "after_sha256": digest(actual),
        "proposal_sha": sha,
        "applied_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "rolled_back": False,
    })
    try:
        github_put(SNAPSHOT_PATH, snapshot_document(actual),
                   "Record applied Fuller Family Test dashboard revision")
        synced = True
    except Exception:
        LOG.warning("Dashboard saved, but post-apply GitHub sync failed")
        synced = False
    return {"views": len(actual["views"]), "backup": name, "synced": synced}


def rollback_last():
    state = state_read()
    name = state.get("backup", "")
    if state.get("rolled_back") or not isinstance(name, str) or not name.endswith(".json"):
        raise BridgeError("There is no active deployment to roll back")
    if os.path.basename(name) != name:
        raise BridgeError("Invalid backup record")
    live = read_dashboard()
    if digest(live) != state.get("after_sha256"):
        raise BridgeError("Dashboard changed after deployment; refusing to overwrite it")
    try:
        with open(os.path.join(BACKUPS_DIR, name), encoding="utf-8") as handle:
            previous = json.load(handle)
    except (OSError, ValueError):
        raise BridgeError("Backup is unavailable or damaged") from None
    if digest(previous) != state.get("before_sha256"):
        raise BridgeError("Backup hash verification failed")
    backup(live)
    save_dashboard(previous, restoring=True)
    if digest(read_dashboard()) != digest(previous):
        raise BridgeError("Restored configuration did not verify")
    state["rolled_back"] = True
    state_write(state)
    try:
        github_put(SNAPSHOT_PATH, snapshot_document(previous),
                   "Record rollback of Fuller Family Test dashboard")
    except Exception:
        LOG.warning("Rollback succeeded, but GitHub snapshot sync failed")
    return {"views": len(previous["views"])}



# Explicit, fixed-scope setup for Heru sleep and humidity actions. This is NOT
# a generic scripting endpoint; a human must click Install Family Controls.
SLEEP_HELPER = "input_boolean.horus_heru_sleeping"
SPOT = "media_player.bedroom_master_bdrm_spot"
HUMIDIFIER = "humidifier.basement_dehumidifier"
AC_UNITS = {
    "living_room": "climate.living_room_living_room_ac",
    "my_bedroom": "climate.bedroom_my_bedroom_ac",
    "dining_room": "climate.dining_room_dining_room_ac",
    "heru_room": "climate.herus_room_herus_ac",
    "guest_room": "climate.guest_bedroom_guest_room_ac",
}
NEST = "climate.nest_learning_thermostat_4th_gen"


def preset_scripts():
    def step(action, entity_id, data=None):
        out = {"action": action, "target": {"entity_id": entity_id}}
        if data is not None:
            out["data"] = data
        return out
    presets = {
        "horus_heru_sleep": {
            "alias": "HORUS - Heru Sleeping", "mode": "single",
            "sequence": [
                step("media_player.volume_set", SPOT, {"volume_level": 0.1}),
                step("input_boolean.turn_on", SLEEP_HELPER),
            ],
        },
        "horus_heru_awake": {
            "alias": "HORUS - Heru Awake", "mode": "single",
            "sequence": [
                step("media_player.volume_set", SPOT, {"volume_level": 0.7}),
                step("input_boolean.turn_off", SLEEP_HELPER),
            ],
        },
        "horus_basement_humidity_up": {
            "alias": "HORUS - Raise Basement Humidity Target", "mode": "single",
            "sequence": [
                step("humidifier.set_humidity", HUMIDIFIER, {
                    "humidity": "{{ [85, ((state_attr('humidifier.basement_dehumidifier','humidity') | float(45)) + 5)] | min | int }}"
                }),
            ],
        },
        "horus_basement_humidity_down": {
            "alias": "HORUS - Lower Basement Humidity Target", "mode": "single",
            "sequence": [
                step("humidifier.set_humidity", HUMIDIFIER, {
                    "humidity": "{{ [35, ((state_attr('humidifier.basement_dehumidifier','humidity') | float(45)) - 5)] | max | int }}"
                }),
            ],
        },
        "horus_nest_temp_up": {
            "alias": "HORUS - Nest Increase Temperature", "mode": "single",
            "sequence": [
                step("climate.set_temperature", "climate.nest_learning_thermostat_4th_gen", {
                    "temperature": "{{ [90, ((state_attr('climate.nest_learning_thermostat_4th_gen', 'temperature') | float(74)) + 1)] | min | int }}"
                }),
            ],
        },
        "horus_nest_temp_down": {
            "alias": "HORUS - Nest Decrease Temperature", "mode": "single",
            "sequence": [
                step("climate.set_temperature", "climate.nest_learning_thermostat_4th_gen", {
                    "temperature": "{{ [50, ((state_attr('climate.nest_learning_thermostat_4th_gen', 'temperature') | float(74)) - 1)] | max | int }}"
                }),
            ],
        },
    }
    for room, entity in AC_UNITS.items():
        for direction, sign in (("up", "+"), ("down", "-")):
            template = (
                "{{ [90, [60, ((state_attr('" + entity +
                "', 'temperature') | float(72)) " + sign + " 1)] | max] | min | int }}"
            )
            presets["horus_ac_" + room + "_" + direction] = {
                "alias": "HORUS - " + room.replace("_", " ").title() +
                         " AC Temperature " + direction.title(),
                "mode": "single",
                "sequence": [step(
                    "climate.set_temperature", entity, {"temperature": template}
                )],
            }
    return presets


def helper_command(command, **args):
    if command == "input_boolean/list":
        if args:
            raise BridgeError("Invalid helper list")
    elif command == "input_boolean/create":
        if args != {"name": "HORUS Heru Sleeping", "icon": "mdi:sleep"}:
            raise BridgeError("Unapproved helper create")
    else:
        raise BridgeError("Unapproved helper command")
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise BridgeError("Supervisor token unavailable")
    from websockets.sync.client import connect
    with connect("ws://supervisor/core/websocket", open_timeout=8, close_timeout=3,
                 max_size=MAX_RESPONSE) as ws:
        if json.loads(ws.recv(timeout=8)).get("type") != "auth_required":
            raise BridgeError("Unexpected helper challenge")
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(ws.recv(timeout=8)).get("type") != "auth_ok":
            raise BridgeError("Home Assistant helper authorization failed")
        ws.send(json.dumps({"id": 1, "type": command, **args}))
        result = json.loads(ws.recv(timeout=15))
        if result.get("type") != "result" or not result.get("success"):
            raise BridgeError("Home Assistant helper request failed")
        return result.get("result")


def script_request(script_id, method="GET", document=None):
    if script_id not in preset_scripts() or method not in ("GET", "POST"):
        raise BridgeError("Unapproved script operation")
    if method == "POST" and document != preset_scripts()[script_id]:
        raise BridgeError("Script differs from approved fixed preset")
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise BridgeError("Supervisor token unavailable")
    req = Request("http://supervisor/core/api/config/script/config/" + script_id,
                  method=method,
                  data=None if document is None else json.dumps(document).encode("utf-8"),
                  headers={"Authorization": "Bearer " + token,
                           "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=20) as response:
            body = response.read(100_000)
            return json.loads(body) if body else {}
    except HTTPError as exc:
        if method == "GET" and exc.code == 404:
            return None
        raise BridgeError("Home Assistant script API HTTP " + str(exc.code)) from None
    except (URLError, TimeoutError):
        raise BridgeError("Home Assistant script API unreachable") from None


def install_family_controls():
    current = helper_command("input_boolean/list")
    entries = current if isinstance(current, list) else (
        current.get("items", []) if isinstance(current, dict) else []
    )
    if not isinstance(entries, list):
        raise BridgeError("Unexpected helpers response")
    helper = next((item for item in entries if isinstance(item, dict)
                   and item.get("id") == "horus_heru_sleeping"), None)
    if helper is not None and helper.get("name") != "HORUS Heru Sleeping":
        raise BridgeError("Existing helper ID belongs to something else")
    if helper is None:
        added = helper_command("input_boolean/create",
                               name="HORUS Heru Sleeping", icon="mdi:sleep")
        if not isinstance(added, dict) or added.get("id") != "horus_heru_sleeping":
            raise BridgeError("Helper ID did not match expected value")
    for key, preset in preset_scripts().items():
        existing = script_request(key)
        if existing is not None:
            # Never silently reuse someone else's script with the same ID.
            config = existing.get("config", existing) if isinstance(existing, dict) else {}
            if not isinstance(config, dict) or any(
                config.get(field) != preset[field] for field in ("alias", "sequence", "mode")
            ):
                raise BridgeError("Existing script conflicts with " + key)
            continue
        script_request(key, "POST", preset)
    return len(preset_scripts())


def required_preset_ids(config):
    presets = preset_scripts()
    found = set()
    def inspect(node):
        if isinstance(node, list):
            for item in node:
                inspect(item)
        elif isinstance(node, dict):
            if node.get("perform_action") == "script.turn_on":
                ids = node.get("target", {}).get("entity_id")
                if isinstance(ids, str) and ids.startswith("script.horus_"):
                    ident = ids.removeprefix("script.")
                    if ident not in presets:
                        raise BridgeError("Unknown HORUS family control in proposal: " + ident)
                    found.add(ident)
            for value in node.values():
                inspect(value)
    inspect(config)
    return found


def validate_required_controls(config):
    requested = required_preset_ids(config)
    if "horus_heru_sleep" in requested or "horus_heru_awake" in requested:
        entries = helper_command("input_boolean/list")
        items = entries if isinstance(entries, list) else (
            entries.get("items", []) if isinstance(entries, dict) else []
        )
        if not any(isinstance(row, dict) and row.get("id") == "horus_heru_sleeping"
                   for row in items):
            raise MissingControls("Heru sleep helper is not installed")
    presets = preset_scripts()
    missing = []
    for script_id in sorted(requested):
        expected = presets[script_id]
        current = script_request(script_id)
        actual = current.get("config", current) if isinstance(current, dict) else {}
        if not isinstance(actual, dict) or any(
            actual.get(field) != expected[field] for field in ("alias", "sequence", "mode")
        ):
            missing.append(script_id)
    if missing:
        raise MissingControls(
            "Required controls need installation or updating: " + ", ".join(missing[:8]) +
            (" (and more)" if len(missing) > 8 else "")
        )


def review_representation(config):
    """Display data-image payloads by content hash, not a wall of URI-encoded SVG.

    The exact unabridged diff is available separately. This is never the
    configuration submitted to Home Assistant.
    """
    def walk(node):
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, dict):
            return {key: walk(value) for key, value in node.items()}
        if isinstance(node, str) and node.startswith("data:image/svg+xml,"):
            if len(node) > 2_000_000:
                raise BridgeError("SVG image exceeds preview limit")
            raw = unquote(node.partition(",")[2])
            fingerprint = hashlib.sha256(node.encode("utf-8")).hexdigest()
            labels = [html.unescape(value).strip()[:75] for value in
                      re.findall(r"<text\b[^>]*>([^<]*)</text>", raw, flags=re.I)]
            label_text = "; ".join(x for x in labels[:12] if x)
            return ("[SVG image: SHA256 " + fingerprint +
                    ", " + str(len(node)) + " chars" +
                    (", visible labels: " + label_text if label_text else "") + "]")
        return node
    return walk(config)


def compare_dashboard_configs(before, after, compact=True):
    old = review_representation(before) if compact else before
    new = review_representation(after) if compact else after
    prior = json.dumps(old, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    proposed = json.dumps(new, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    return "\n".join(difflib.unified_diff(
        prior, proposed, fromfile="CURRENT", tofile="PROPOSED", lineterm=""))


def page(title, content):
    # Zero-JavaScript, CSP-compatible Home Assistant ingress interface.
    style = """*{box-sizing:border-box}body{font:16px/1.5 system-ui,-apple-system,BlinkMacSystemFont,
    "Segoe UI",sans-serif;margin:0;min-height:100vh;padding:clamp(18px,4vw,44px);
    color:#edf7ff;background:radial-gradient(circle at 75% 0%,#123e68 0%,transparent 42%),
    linear-gradient(140deg,#08111f 0%,#0d1c30 55%,#06101d 100%)}
    body:before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
    background-image:linear-gradient(rgba(60,112,170,.045) 1px,transparent 1px),
    linear-gradient(90deg,rgba(60,112,170,.045) 1px,transparent 1px);background-size:46px 46px}
    main{max-width:940px;margin:0 auto;background:linear-gradient(145deg,#16273d,#0e1a2b);
    padding:clamp(22px,3vw,36px);border:1px solid #365e81;border-radius:22px;
    box-shadow:0 18px 70px #020814a8,inset 0 1px 0 #44668855;position:relative;overflow:hidden}
    main:before{content:"";height:4px;position:absolute;top:0;left:0;right:0;
    background:linear-gradient(90deg,#61eabc,#5ad5ff 50%,#b7a0fa)}
    .brand{display:flex;align-items:center;gap:13px;margin-bottom:22px}
    .mark{width:49px;height:49px;display:grid;place-items:center;border-radius:13px;
    color:#79eeff;background:#10364d;border:1px solid #55d3f699;
    box-shadow:0 0 25px #16b6e42c;font-weight:900;font-size:23px;letter-spacing:-3px}
    .eyebrow{margin:0;color:#68c9eb;letter-spacing:2px;font-size:11px;font-weight:700}
    h1{font-size:clamp(23px,3vw,31px);line-height:1.18;margin:3px 0 0;letter-spacing:.2px}
    h2{font-size:14px;letter-spacing:1.5px;text-transform:uppercase;color:#84ddec;margin:0 0 14px}
    p{margin:13px 0;color:#c9d8e6}.muted{color:#9fb3c8;font-size:13px}
    strong{color:#eff8ff}.status{display:inline-block;border:1px solid #37677b;
    color:#96eeff;background:#143247;border-radius:99px;padding:5px 12px;
    font-size:12px;font-weight:700;letter-spacing:.5px}
    .module{padding:18px;margin:15px 0;border-radius:16px;border:1px solid #304c66;
    background:linear-gradient(115deg,#172d46,#14243a 70%)}
    .module.good{border-color:#3a846e}.module.alert{border-color:#986358}
    .actions{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:18px 0}
    form{margin:0}.actions form{display:block}
    .control,button{display:flex;align-items:center;justify-content:center;text-align:center;
    gap:12px;min-height:57px;width:100%;padding:14px 17px;border-radius:13px;
    text-decoration:none;font-size:15px;font-family:inherit;font-weight:750;
    letter-spacing:.2px;line-height:1.4;cursor:pointer;transition:background .15s,border-color .15s;
    background:#163e5c;color:#e6faff;border:1px solid #4d93b5;box-shadow:inset 0 1px 0 #ffffff18}
    .control:hover,button:hover{background:#205375;border-color:#6ae3f1}
    .control:focus-visible,button:focus-visible{outline:3px solid #77f2dd;outline-offset:3px}
    .primary{background:linear-gradient(110deg,#125c69,#176882);border-color:#5adbd2;color:#f2ffff}
    .primary:hover{background:#1b7887}
    .danger{background:#48232d;border-color:#cb6973;color:#fff0f2}.danger:hover{background:#713340}
    .secondary{background:#23344d;border-color:#5b7590;color:#edf5ff}
    .control:before,button:before{content:"";width:8px;height:8px;border-radius:3px;
    flex:0 0 auto;background:#70e8ee;box-shadow:0 0 12px #70e8ee55}
    .danger:before{background:#ff8c9c;box-shadow:0 0 12px #f7889a55}
    button:disabled{opacity:.48;cursor:not-allowed}.module input{color:inherit}
    details{margin:17px 0;border:1px solid #47617d;border-radius:12px;background:#101d30}summary{padding:16px 18px;cursor:pointer;color:#8de3ed;font-weight:700}details[open] summary{border-bottom:1px solid #385672}details pre{margin:12px}
    pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:55vh;overflow:auto;
    font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace;background:#071322;
    color:#cbe7fb;border:1px solid #42607a;padding:17px;border-radius:12px}
    code{color:#97eafe}a:not(.control){color:#83d9ff}
    @media(max-width:520px){.actions{grid-template-columns:1fr}.brand{align-items:flex-start}}
    """
    body = ('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta charset="utf-8"><title>' + html.escape(title) + '</title>'
            '<style>' + style + '</style></head><body><main><header class="brand">'
            '<div class="mark">H</div><div><p class="eyebrow">HORUS / FULLER FAMILY</p><h1>' +
            html.escape(title) + '</h1></div></header>' + content + '</main></body></html>')
    return body.encode("utf-8")


def form_button(action, label, extra="", danger=False):
    if action not in ("/sync", "/apply", "/rollback", "/setup-household"):
        raise ValueError("Invalid form action")
    return ('<form action="' + html.escape(action.lstrip("/"), quote=True) + '" method="post">'
            '<input type="hidden" name="csrf" value="' + html.escape(CSRF, quote=True) + '">'
            + extra + '<button class="control ' + ("danger" if danger else "primary") +
            '" type="submit">' + html.escape(label) + '</button></form>')


def action_link(path, label, secondary=False):
    if path not in ("test", "preview", "full-diff", "./"):
        raise ValueError("Invalid navigation target")
    return ('<a class="control ' + ("secondary" if secondary else "primary") +
            '" href="' + html.escape(path, quote=True) + '">' +
            html.escape(label) + '</a>')


class Handler(BaseHTTPRequestHandler):
    def allowed(self):
        # Requests must pass through Home Assistant's admin-only ingress proxy.
        if self.client_address[0] != "172.30.32.2":
            self.respond(403, json.dumps({"error": "ingress_required"}).encode("utf-8"), "application/json")
            return False
        return True

    def respond(self, status, body, media="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", media)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def show(self, status, title, content):
        self.respond(status, page(title, content))

    def do_GET(self):
        if not self.allowed():
            return
        url = urlsplit(self.path)
        if url.query or url.fragment:
            self.show(400, "Invalid request", "<p>Query parameters are not supported.</p>")
            return
        if url.path == "/health":
            info = {"status": "running", "mode": "single-dashboard", "target": PATH, "version": "0.2.9"}
            self.respond(200, json.dumps(info).encode("utf-8"), "application/json")
        elif url.path == "/test":
            try:
                current = read_dashboard()
                content = ('<div class="module good"><span class="status">CONNECTED</span>'
                           '<p><strong>FULLER FAMILY TEST</strong></p>'
                           '<p>' + str(len(current["views"])) + ' views available</p></div>'
                           '<div class="actions">' + action_link("./", "Dashboard Manager",
                           secondary=True) + '</div>')
                self.show(200, "Connection Test", content)
            except Exception as exc:
                content = ('<div class="module alert"><span class="status">CONNECTION FAILED</span>'
                           '<p>' + html.escape(str(exc)) + '</p></div><div class="actions">'
                           + action_link("./", "Dashboard Manager", secondary=True) + '</div>')
                self.show(503, "Connection Test", content)
        elif url.path == "/":
            try:
                enabled = bool(github_token())
            except BridgeError:
                enabled = False
            state = state_read()
            content = ('<div class="module good"><span class="status">SINGLE DASHBOARD</span>'
                       '<p><strong>FULLER FAMILY TEST</strong></p></div>')
            if not enabled:
                content += ('<div class="module alert"><h2>Connect Private GitHub</h2>'
                            '<p>Set the GitHub token under the add-on Configuration tab, save, '
                            'then restart the add-on.</p></div>')
                content += '<div class="actions">' + action_link("test", "Test connection") + '</div>'
            else:
                content += ('<div class="module"><h2>Dashboard Controls</h2><div class="actions">'
                            + form_button("/sync", "Sync dashboard to GitHub")
                            + action_link("preview", "Preview proposed update")
                            + action_link("test", "Test connection", secondary=True)
                            + '</div></div>')
                content += ('<div class="module"><h2>Family Controls</h2>'
                            '<p class="muted">Create or verify the Heru, basement humidity, Nest and Frigidaire AC controls. '
                            'No device action occurs until a dashboard button is pressed.</p>'
                            '<div class="actions">'
                            + form_button("/setup-household", "Install / Update Family Controls")
                            + '</div></div>')
                if state.get("backup") and not state.get("rolled_back"):
                    content += ('<div class="module"><h2>Recovery</h2><div class="actions">'
                                + form_button("/rollback", "Restore previous dashboard", danger=True)
                                + '</div></div>')
            content += ('<p class="muted">Only Fuller Family Test can be changed. '
                        'A proposal requires your approval before saving.</p>')
            self.show(200, "Dashboard Manager", content)
        elif url.path == "/preview":
            with LOCK:
                try:
                    proposed, sha = load_proposal()
                    live = read_dashboard()
                    require_fresh(proposed, live)
                    display_diff = compare_dashboard_configs(live, proposed["config"], compact=True)
                    if len(display_diff) > 240_000:
                        raise BridgeError("Compact preview is too large; narrow the proposed update")
                    extra = ('<input type="hidden" name="proposal_sha" value="' +
                             html.escape(sha, quote=True) + '">')
                    content = ('<p><strong>Change:</strong> ' + html.escape(proposed["summary"]) + '</p>'
                               '<p>Current views: ' + str(len(live["views"])) +
                               ' | Proposed views: ' + str(len(proposed["config"]["views"])) + '</p>'
                               '<details><summary>Review changes (embedded graphics summarized by SHA-256)</summary><pre>' +
                               html.escape(display_diff) + '</pre></details>' +
                               '<p class="muted">SVG graphics are summarized here for readability. '
                               'Their full exact content can be reviewed separately.</p>' +
                               '<div class="actions">' +
                               action_link("full-diff", "View complete, unabridged diff", secondary=True) +
                               '</div>' +
                               '<div class="actions">' + form_button("/apply", "Approve & Apply", extra) + '</div>' +
                               '<div class="actions">' + action_link("./", "Cancel", secondary=True) + '</div>')
                    self.show(200, "Review Fuller Family Test update", content)
                except MissingControls as exc:
                    self.show(409, "Install Required Controls",
                              "<div class='module alert'><p>" + html.escape(str(exc)) +
                              "</p><p>These scripts are not installed yet. Setting up controls does not "
                              "change an AC or thermostat until you press a dashboard control.</p></div>"
                              '<div class="actions">' +
                              form_button("/setup-household", "Install Required Controls") +
                              action_link("./", "Cancel", secondary=True) + '</div>')
                except Exception as exc:
                    self.show(409, "Not ready to apply",
                              "<p>" + html.escape(str(exc)) + '</p><div class="actions">' + action_link("./", "Back", secondary=True) + '</div>')
        elif url.path == "/full-diff":
            with LOCK:
                try:
                    proposed, sha = load_proposal()
                    live = read_dashboard()
                    require_fresh(proposed, live)
                    exact = compare_dashboard_configs(live, proposed["config"], compact=False)
                    if len(exact) > 2_500_000:
                        raise BridgeError("Full diff exceeds rendering limit")
                    content = ('<p>Unabridged comparison of the live dashboard and '
                               'the proposed GitHub revision. Nothing is omitted.</p>'
                               '<p class="muted">Proposal file SHA: ' +
                               html.escape(sha) + '</p><pre>' + html.escape(exact) +
                               '</pre><div class="actions">' +
                               action_link("preview", "Back to review", secondary=True) +
                               '</div>')
                    self.show(200, "Full Dashboard Diff", content)
                except Exception as exc:
                    self.show(409, "Full Diff Unavailable",
                              "<p>" + html.escape(str(exc)) +
                              '</p><div class="actions">' +
                              action_link("preview", "Back to review", secondary=True) +
                              '</div>')
        else:
            self.show(404, "Not found", "<p>That action is not available.</p>")

    def do_POST(self):
        if not self.allowed():
            return
        route = urlsplit(self.path)
        if route.path not in ("/sync", "/apply", "/rollback", "/setup-household") or route.query:
            self.show(405, "Not allowed", "<p>No other write operations are supported.</p>")
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 1 or size > 4096:
                raise BridgeError("Invalid request length")
            data = parse_qs(self.rfile.read(size).decode("utf-8"), keep_blank_values=True)
            supplied = data.get("csrf", [""])[0]
            if not hmac.compare_digest(CSRF, supplied):
                raise BridgeError("Approval form expired. Reopen the manager.")
            with LOCK:
                if route.path == "/sync":
                    result = sync_to_github()
                    title = "Dashboard synced"
                    message = ("<p>Current dashboard safely copied to the private GitHub repository. "
                               "Views: " + str(result["views"]) + ".</p>"
                               "<p>ChatGPT can now read the snapshot and prepare a proposal.</p>")
                elif route.path == "/setup-household":
                    count = install_family_controls()
                    title = "Family Controls Ready"
                    message = ("<p>Heru, Nest, humidity, and room AC controls are installed (" + str(count) +
                               " fixed scripts).</p><div class='actions'>" +
                               action_link("preview", "Preview Dashboard Update") + "</div>")
                elif route.path == "/apply":
                    sha = data.get("proposal_sha", [""])[0]
                    result = apply_proposal(sha)
                    title = "Dashboard updated"
                    message = ("<p>Fuller Family Test saved and verified with " +
                               str(result["views"]) + " view(s). Automatic backup created.</p>")
                    if not result["synced"]:
                        message += "<p>GitHub post-deployment sync failed. Use Sync current dashboard.</p>"
                else:
                    result = rollback_last()
                    title = "Dashboard restored"
                    message = ("<p>Last deployed dashboard restored and verified. Views: " +
                               str(result["views"]) + ".</p>")
            LOG.info("%s succeeded (Fuller Family Test only)", route.path)
            self.show(200, title, message + '<div class="actions">' + action_link("./", "Dashboard Manager", secondary=True) + '</div>')
        except Exception as exc:
            LOG.warning("Action %s failed: %s", route.path, type(exc).__name__)
            detail = str(exc) if isinstance(exc, BridgeError) else "Unexpected operation failure; inspect add-on logs"
            self.show(409, "Action failed", "<p>" + html.escape(detail) +
                      '</p><div class="actions">' + action_link("./", "Dashboard Manager", secondary=True) + '</div>')

    def do_PUT(self):
        self.show(405, "Not allowed", "<p>Only approved UI actions are supported.</p>")

    do_PATCH = do_PUT
    do_DELETE = do_PUT


if __name__ == "__main__":
    LOG.setLevel(logging.INFO)
    ThreadingHTTPServer(("0.0.0.0", 8099), Handler).serve_forever()
