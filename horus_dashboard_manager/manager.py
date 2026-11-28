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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlsplit
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asset_manager as assets
import asset_bridge
import gallery_import
import gallery_curator
import nanit_install
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
    # Allow confirmed unlocks for exactly the two registered Fuller Family door locks.
    # Never permit an unconfirmed unlock, arbitrary lock target, or alarm disarm shortcut.
    allowed_unlock_entities = frozenset((
        "lock.front_of_house_front_door",
        "lock.back_door",
    ))
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
                            and node.get("target") in ({"entity_id": entity} for entity in allowed_unlock_entities)
                            and isinstance(node.get("confirmation"), dict)
                            and bool(str(node["confirmation"].get("text", "")).strip())
                            and node.get("data") in (None, {})
                        )
                        if not allowed:
                            raise BridgeError("Unlock requires confirmation and the exact front-door or back-door entity")
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
        raise BridgeError("Only Fuller Family may be accessed")
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


def github_request(path, method="GET", payload=None, *, ref=None):
    token = github_token()
    api = "https://api.github.com/repos/" + PRIVATE_REPO + "/contents/" + quote(path, safe="/")
    if method == "GET":
        api += "?ref=" + (ref or BRANCH)
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
            raw = response.read(2_500_001)
            if len(raw) > 2_500_000:
                raise BridgeError("GitHub response exceeds limit")
            return json.loads(raw)
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
    record = github_request(path)
    sha = None
    if record is not None:
        if not isinstance(record, dict) or not record.get("sha"):
            raise BridgeError("Missing GitHub file revision")
        sha = record["sha"]
    payload = {
        "message": message,
        "branch": BRANCH,
        "content": base64.b64encode(
            (json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
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
    if "assets" in document:
        assets.manifest(document["assets"])
    validate_required_controls(document["config"])
    return document, sha


def require_fresh(proposal, live):
    if not hmac.compare_digest(proposal["base_sha256"], digest(live)):
        raise BridgeError("Dashboard changed since sync. Sync again before applying.")
    if digest(proposal["config"]) == digest(live) and not proposal.get("assets"):
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


def apply_proposal(requested_sha, requested_assets=None):
    proposal, sha = load_proposal()
    if not isinstance(requested_sha, str) or not hmac.compare_digest(sha, requested_sha):
        raise BridgeError("Proposal changed since preview. Review the new version first.")
    before = read_dashboard()
    require_fresh(proposal, before)
    plan_hash, _ = asset_bridge.prepare(proposal, before)
    if plan_hash and (not isinstance(requested_assets, str) or not hmac.compare_digest(plan_hash, requested_assets)):
        raise BridgeError("Asset state changed since preview; review again")
    prior_hash = digest(before)
    name = backup(before)
    target = proposal["config"]
    asset_backup = None
    context = {"backup": name, "before_sha256": prior_hash, "after_sha256": digest(target), "proposal_sha": sha}
    save_attempted = False
    try:
        if plan_hash:
            asset_backup = assets.install(proposal["assets"], plan_hash, github_request, asset_bridge.resource, context)
        require_fresh(proposal, read_dashboard())
        if digest(target) != prior_hash:
            save_attempted = True
            save_dashboard(target)
        actual = read_dashboard()
        if digest(actual) != digest(target):
            raise BridgeError("Saved dashboard did not match the proposed version")
    except Exception:
        # Never overwrite an unrelated live edit, including during asset download.
        try:
            live_hash = digest(read_dashboard())
            if save_attempted and live_hash == digest(target):
                save_dashboard(before, restoring=True)
                live_hash = digest(read_dashboard())
            if asset_backup is not None and (live_hash == prior_hash or not save_attempted):
                assets.restore(asset_backup, asset_bridge.resource)
            if save_attempted and live_hash != prior_hash:
                LOG.error("Dashboard changed unexpectedly; automatic rollback refused")
        except Exception:
            LOG.exception("Automatic rollback failed; use recovery")
        raise
    state_write({
        "asset_backup": asset_backup,
        "backup": name,
        "before_sha256": prior_hash,
        "after_sha256": digest(actual),
        "proposal_sha": sha,
        "applied_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "rolled_back": False,
    })
    if asset_backup is not None:
        assets.finish(asset_backup)
    try:
        github_put(SNAPSHOT_PATH, snapshot_document(actual),
                   "Record applied Fuller Family Test dashboard revision")
        synced = True
    except Exception:
        LOG.warning("Dashboard saved, but post-apply GitHub sync failed")
        synced = False
    return {"views": len(actual["views"]), "backup": name, "synced": synced}


def recover_assets():
    ids = assets.pending()
    if len(ids) != 1:
        raise BridgeError("Expected exactly one interrupted asset transaction")
    transaction = ids[0]
    context = assets.load(transaction).get("context", {})
    name = context.get("backup")
    if not isinstance(name, str) or os.path.basename(name) != name or not name.endswith(".json"):
        raise BridgeError("Recovery dashboard backup reference invalid")
    with open(os.path.join(BACKUPS_DIR, name), encoding="utf-8") as handle:
        previous = json.load(handle)
    before = context.get("before_sha256")
    if digest(previous) != before:
        raise BridgeError("Recovery dashboard backup hash mismatch")
    live = read_dashboard()
    actual = digest(live)
    if actual not in (before, context.get("after_sha256")):
        raise BridgeError("Dashboard changed independently; recovery refused")
    assets.check_restore(transaction, asset_bridge.resource)
    if actual != before:
        backup(live)
        save_dashboard(previous, restoring=True)
        if digest(read_dashboard()) != before:
            raise BridgeError("Dashboard recovery did not verify")
    assets.restore(transaction, asset_bridge.resource)
    state = state_read()
    if state.get("asset_backup") == transaction:
        state["rolled_back"] = True
        state_write(state)
    try:
        github_put(SNAPSHOT_PATH, snapshot_document(previous), "Record interrupted HORUS asset recovery")
    except Exception:
        LOG.warning("Recovered dashboard; GitHub snapshot sync failed")
    return len(previous["views"])


def rollback_last():
    if assets.pending():
        raise BridgeError("Recover interrupted asset transaction first")
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
    if state.get("asset_backup"):
        assets.check_restore(state["asset_backup"], asset_bridge.resource)
        assets.mark_pending(state["asset_backup"])
    backup(live)
    save_dashboard(previous, restoring=True)
    if digest(read_dashboard()) != digest(previous):
        raise BridgeError("Restored configuration did not verify")
    if state.get("asset_backup"):
        assets.restore(state["asset_backup"], asset_bridge.resource)
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
    presets.update({
    "horus_watch_netflix": {
        "alias": "HORUS - Watch Netflix",
        "mode": "restart",
        "sequence": [
            {
                "if": [
                    {
                        "condition": "state",
                        "entity_id": "media_player.living_room_the_tv_in_the_living_room_3",
                        "state": "off"
                    }
                ],
                "then": [
                    {
                        "action": "media_player.turn_on",
                        "target": {
                            "entity_id": "media_player.living_room_the_tv_in_the_living_room_3"
                        }
                    }
                ]
            },
            {
                "if": [
                    {
                        "condition": "state",
                        "entity_id": "media_player.living_room_the_tv_in_the_living_room",
                        "state": "off"
                    }
                ],
                "then": [
                    {
                        "action": "remote.turn_on",
                        "target": {
                            "entity_id": "remote.living_room_the_tv_in_the_living_room"
                        }
                    }
                ]
            },
            {
                "wait_template": "{{ states('media_player.living_room_the_tv_in_the_living_room') not in ['off','unavailable','unknown'] and 'Netflix' in (state_attr('media_player.living_room_the_tv_in_the_living_room', 'source_list') or []) }}",
                "timeout": "00:00:30",
                "continue_on_timeout": False
            },
            {
                "action": "media_player.select_source",
                "target": {
                    "entity_id": "media_player.living_room_the_tv_in_the_living_room"
                },
                "data": {
                    "source": "Netflix"
                }
            }
        ]
    },
    "horus_watch_prime": {
        "alias": "HORUS - Watch Prime Video",
        "mode": "restart",
        "sequence": [
            {
                "if": [
                    {
                        "condition": "state",
                        "entity_id": "media_player.living_room_the_tv_in_the_living_room_3",
                        "state": "off"
                    }
                ],
                "then": [
                    {
                        "action": "media_player.turn_on",
                        "target": {
                            "entity_id": "media_player.living_room_the_tv_in_the_living_room_3"
                        }
                    }
                ]
            },
            {
                "if": [
                    {
                        "condition": "state",
                        "entity_id": "media_player.living_room_the_tv_in_the_living_room",
                        "state": "off"
                    }
                ],
                "then": [
                    {
                        "action": "remote.turn_on",
                        "target": {
                            "entity_id": "remote.living_room_the_tv_in_the_living_room"
                        }
                    }
                ]
            },
            {
                "wait_template": "{{ states('media_player.living_room_the_tv_in_the_living_room') not in ['off','unavailable','unknown'] and 'Prime Video' in (state_attr('media_player.living_room_the_tv_in_the_living_room', 'source_list') or []) }}",
                "timeout": "00:00:30",
                "continue_on_timeout": False
            },
            {
                "action": "media_player.select_source",
                "target": {
                    "entity_id": "media_player.living_room_the_tv_in_the_living_room"
                },
                "data": {
                    "source": "Prime Video"
                }
            }
        ]
    },
    "horus_watch_hbo": {
        "alias": "HORUS - Watch HBO Max",
        "mode": "restart",
        "sequence": [
            {
                "if": [
                    {
                        "condition": "state",
                        "entity_id": "media_player.living_room_the_tv_in_the_living_room_3",
                        "state": "off"
                    }
                ],
                "then": [
                    {
                        "action": "media_player.turn_on",
                        "target": {
                            "entity_id": "media_player.living_room_the_tv_in_the_living_room_3"
                        }
                    }
                ]
            },
            {
                "if": [
                    {
                        "condition": "state",
                        "entity_id": "media_player.living_room_the_tv_in_the_living_room",
                        "state": "off"
                    }
                ],
                "then": [
                    {
                        "action": "remote.turn_on",
                        "target": {
                            "entity_id": "remote.living_room_the_tv_in_the_living_room"
                        }
                    }
                ]
            },
            {
                "wait_template": "{{ states('media_player.living_room_the_tv_in_the_living_room') not in ['off','unavailable','unknown'] and 'HBO Max' in (state_attr('media_player.living_room_the_tv_in_the_living_room', 'source_list') or []) }}",
                "timeout": "00:00:30",
                "continue_on_timeout": False
            },
            {
                "action": "media_player.select_source",
                "target": {
                    "entity_id": "media_player.living_room_the_tv_in_the_living_room"
                },
                "data": {
                    "source": "HBO Max"
                }
            }
        ]
    }
})
    presets["horus_doorbell_live_view"] = {"alias":"HORUS Doorbell Live View","mode":"single","max_exceeded":"silent","sequence":[{"if":[{"condition":"state","entity_id":"switch.entryway_fuller_family_kiosk_screen","state":"off"}],"then":[{"action":"switch.turn_on","target":{"entity_id":"switch.entryway_fuller_family_kiosk_screen"}}]},{"delay":"00:00:05"}]}
    return presets


def helper_command(command, **args):
    if command == "input_boolean/list":
        if args:
            raise BridgeError("Invalid helper list")
    elif command == "input_boolean/create":
        if args not in ({"name": "HORUS Heru Sleeping", "icon": "mdi:sleep"},
                        {"name": "HORUS Living Room Streaming", "icon": "mdi:television-play"},
                        {"name": "HORUS Bedroom Streaming", "icon": "mdi:television-play"}):
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


KIOSK_DEVICE = "e8969f67449f17aa9e2c94db1e099018"
KIOSK_URL = "http://192.168.1.208/fuller-family-test/kiosk-security?horus_kiosk=1&hide_sidebar&kiosk"


SCHLAGE_POLL_ID = "horus_schlage_fast_polling"
SCHLAGE_POLL_ENTITY = "automation.horus_schlage_fast_polling"


def schlage_poll_config():
    # One front-door refresh updates the shared Schlage account coordinator.
    # Leave native polling enabled as a fallback; do not send lock commands.
    return {"alias": "HORUS Schlage Fast Polling",
            "description": "Owner-approved 10-second Schlage state refresh trial; native polling remains enabled.",
            "mode": "single", "max_exceeded": "silent",
            "triggers": [{"trigger": "time_pattern", "seconds": "/10"}],
            "conditions": [{"condition": "template", "value_template": "{{ states('lock.front_of_house_front_door') not in ['unknown','unavailable'] and states('lock.back_door') not in ['unknown','unavailable'] }}"}],
            "actions": [{"action": "homeassistant.update_entity",
                         "target": {"entity_id": "lock.front_of_house_front_door"}}]}


def schlage_api(path, method="GET", document=None, missing=False):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise BridgeError("Supervisor token unavailable")
    req = Request("http://supervisor/core/api/" + path, method=method,
                  headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                  data=None if document is None else json.dumps(document).encode("utf-8"))
    try:
        with urlopen(req, timeout=20) as response:
            return json.loads(response.read(100000))
    except HTTPError as exc:
        if missing and exc.code == 404:
            return None
        raise BridgeError("Schlage trial request failed") from None


def schlage_poll_existing():
    result = schlage_api("config/automation/config/" + SCHLAGE_POLL_ID, missing=True)
    if result is not None:
        result = result.get("config", result)
        # HA may add the ID to the returned document.
        result = {k:v for k,v in result.items() if k != "id"}
        if result != schlage_poll_config():
            raise BridgeError("Existing polling automation differs; preserved")
    return result


def schlage_poll_state():
    result = schlage_api("states/" + SCHLAGE_POLL_ENTITY, missing=True)
    if result is not None and result.get("attributes", {}).get("id") != SCHLAGE_POLL_ID:
        raise BridgeError("Automation entity collision; no changes made")
    return result.get("state", "unknown") if result else "not installed"


def set_schlage_polling(enabled):
    existing = schlage_poll_existing()
    current = schlage_poll_state()
    if not enabled and existing is None:
        return "not installed"
    if existing is None:
        if current != "not installed":
            raise BridgeError("Automation entity collision; no changes made")
        schlage_api("config/automation/config/" + SCHLAGE_POLL_ID, "POST", schlage_poll_config())
        if schlage_poll_existing() is None:
            raise BridgeError("Polling automation creation not verified")
    for _ in range(5):
        current = schlage_poll_state()
        if current in ("on", "off"):
            break
        time.sleep(1)
    if current not in ("on", "off"):
        raise BridgeError("Polling automation state unavailable; inspect Home Assistant")
    desired = "on" if enabled else "off"
    if current != desired:
        schlage_api("services/automation/turn_" + desired, "POST", {"entity_id": SCHLAGE_POLL_ENTITY})
    if schlage_poll_state() != desired:
        raise BridgeError("Polling automation state not verified")
    return desired


WATCHDOG_PATH = "/data/horus_kiosk_watchdog.enabled"
WATCHDOG_EVENT = "horus_kiosk_heartbeat"
WATCHDOG_PHASES = frozenset(("healthy", "protected", "main_stale", "render_stale"))
WATCHDOG_STATUS = {"connected": False, "phase": "waiting", "last_heartbeat": None,
                   "last_recovery": None, "recoveries": 0}
WATCHDOG_STATE_LOCK = threading.Lock()


class KioskWatchdog:
    def __init__(self):
        self.last = None
        self.phase = "waiting"
        self.bad = 0
        self.last_recovery = None
        self.attempts = []

    def beat(self, phase, now):
        if phase not in WATCHDOG_PHASES:
            return False
        if self.last is not None and now - self.last < 2:
            return False
        self.last = now
        self.phase = phase
        self.bad = self.bad + 1 if phase in ("main_stale", "render_stale") else 0
        return True

    def reason(self, now):
        # Require a real heartbeat before arming; no false recovery of old clients.
        if self.last is None or self.phase == "protected":
            return None
        if self.last_recovery is not None and now - self.last_recovery < 300:
            return None
        self.attempts = [t for t in self.attempts if now - t < 3600]
        if len(self.attempts) >= 3:
            return None
        if now - self.last >= 15:
            return "missing heartbeat"
        if self.bad >= 2:
            return self.phase
        return None

    def recovered(self, now):
        self.last_recovery = now
        self.attempts.append(now)
        self.bad = 0


def watchdog_enabled():
    return os.path.isfile(WATCHDOG_PATH)


def set_watchdog(enabled):
    if enabled:
        with WATCHDOG_STATE_LOCK:
            if not WATCHDOG_STATUS["connected"] or not WATCHDOG_STATUS["last_heartbeat"]:
                raise BridgeError("Apply the watchdog dashboard first and wait for a live kiosk heartbeat")
        with open(WATCHDOG_PATH, "w", encoding="utf-8") as handle:
            handle.write("owner approved\n")
    elif os.path.exists(WATCHDOG_PATH):
        os.unlink(WATCHDOG_PATH)


def watchdog_guard():
    # Never interrupt the alarm, doorbell, sleep, maintenance, or a different page.
    def state(entity):
        return schlage_api("states/" + entity)
    prefix = "entryway_fuller_family_kiosk_"
    if state("switch."+prefix+"screen").get("state") != "on":
        return False
    if state("switch."+prefix+"screensaver").get("state") != "off":
        return False
    if state("switch."+prefix+"maintenance_mode").get("state") != "off":
        return False
    url = state("sensor."+prefix+"current_page").get("state")
    if url != KIOSK_URL:
        return False
    if state("alarm_control_panel.home_alarm").get("state") not in ("disarmed","armed_home","armed_away","armed_night","armed_vacation","armed_custom_bypass"):
        return False
    doorbell = state("script.horus_doorbell_live_view")
    if doorbell.get("state") != "off":
        return False
    stamp = doorbell.get("attributes",{}).get("last_triggered")
    cache = state("button."+prefix+"clear_browser_cache").get("state")
    for value in (stamp,cache):
        if value and value not in ("unknown","unavailable"):
            age = (datetime.datetime.now(datetime.timezone.utc)-datetime.datetime.fromisoformat(value.replace("Z","+00:00"))).total_seconds()
            if age < 60:
                return False
    return True


def watchdog_worker():
    from websockets.sync.client import connect
    while True:
        try:
            token = os.environ.get("SUPERVISOR_TOKEN")
            if not token:
                raise BridgeError("Supervisor token unavailable")
            with connect("ws://supervisor/core/websocket",open_timeout=8,close_timeout=3,max_size=MAX_RESPONSE) as ws:
                if json.loads(ws.recv(timeout=8)).get("type") != "auth_required":
                    raise BridgeError("Watchdog challenge failed")
                ws.send(json.dumps({"type":"auth","access_token":token}))
                if json.loads(ws.recv(timeout=8)).get("type") != "auth_ok":
                    raise BridgeError("Watchdog authorization failed")
                ws.send(json.dumps({"id":1,"type":"subscribe_events","event_type":WATCHDOG_EVENT}))
                if not json.loads(ws.recv(timeout=8)).get("success"):
                    raise BridgeError("Watchdog subscription failed")
                monitor = KioskWatchdog()
                with WATCHDOG_STATE_LOCK:
                    WATCHDOG_STATUS.update(connected=True,phase="waiting",last_heartbeat=None)
                next_check = time.monotonic()
                while True:
                    try:
                        msg = json.loads(ws.recv(timeout=max(.05,min(5,next_check-time.monotonic()))))
                        event = msg.get("event",{})
                        data = event.get("data",{})
                        if event.get("event_type") == WATCHDOG_EVENT and data.get("version") == 1 and monitor.beat(data.get("phase"),time.monotonic()):
                            with WATCHDOG_STATE_LOCK:
                                WATCHDOG_STATUS.update(phase=monitor.phase,last_heartbeat=datetime.datetime.now(datetime.timezone.utc).isoformat())
                    except TimeoutError:
                        pass
                    now = time.monotonic()
                    if now < next_check:
                        continue
                    next_check = now + 5
                    reason = monitor.reason(now)
                    if reason and watchdog_enabled() and LOCK.acquire(blocking=False):
                        try:
                            if watchdog_enabled() and watchdog_guard():
                                monitor.recovered(now)
                                ok = refresh_kiosk_after_apply()
                                with WATCHDOG_STATE_LOCK:
                                    WATCHDOG_STATUS["last_recovery"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                                    WATCHDOG_STATUS["recoveries"] += 1
                                LOG.warning("Kiosk watchdog recovery: %s; commands accepted=%s",reason,ok)
                        except Exception:
                            LOG.warning("Kiosk watchdog recovery guard failed; no further action")
                        finally:
                            LOCK.release()
        except Exception as exc:
            with WATCHDOG_STATE_LOCK:
                WATCHDOG_STATUS.update(connected=False,phase="disconnected",last_heartbeat=None)
            LOG.warning("Kiosk watchdog connection unavailable: %s",type(exc).__name__)
            time.sleep(5)


def refresh_kiosk_after_apply():
    """Owner-tested full webpage navigation; never exit Fully Kiosk or reboot."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return False
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    def service(action, data):
        for attempt in range(2):
            try:
                req = Request("http://supervisor/core/api/services/" + action,
                              method="POST", data=json.dumps(data).encode("utf-8"),
                              headers=headers)
                with urlopen(req, timeout=15) as response:
                    response.read(100000)
                return True
            except Exception:
                LOG.warning("Post-apply kiosk refresh failed: %s attempt %d", action, attempt+1)
                if attempt == 0:
                    time.sleep(2)
        return False
    cleared = service("button/press", {"entity_id": "button.entryway_fuller_family_kiosk_clear_browser_cache"})
    time.sleep(2)
    blanked = False
    returned = False
    try:
        blanked = service("fully_kiosk/load_url", {"device_id": KIOSK_DEVICE, "url": "about:blank"})
        time.sleep(2)
    finally:
        returned = service("fully_kiosk/load_url", {"device_id": KIOSK_DEVICE, "url": KIOSK_URL})
        if not returned:
            returned = service("button/press", {"entity_id": "button.entryway_fuller_family_kiosk_load_start_url"})
    return cleared and blanked and returned


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
    # Optional, independently toggled streaming-library sections. Exact IDs only.
    for helper_id, helper_name in (
        ("horus_living_room_streaming", "HORUS Living Room Streaming"),
        ("horus_bedroom_streaming", "HORUS Bedroom Streaming"),
    ):
        existing = next((item for item in entries if isinstance(item, dict)
                         and item.get("id") == helper_id), None)
        if existing is not None and existing.get("name") != helper_name:
            raise BridgeError("Existing helper ID belongs to something else: " + helper_id)
        if existing is None:
            added = helper_command("input_boolean/create",
                                   name=helper_name, icon="mdi:television-play")
            if not isinstance(added, dict) or added.get("id") != helper_id:
                raise BridgeError("Streaming helper ID did not match expected value")
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
    # Streaming menus are hidden unless their exact, separately owned helper exists.
    helper_ids = {"horus_living_room_streaming", "horus_bedroom_streaming"}
    serialized = canonical(config)
    needed = {ident for ident in helper_ids if "input_boolean." + ident in serialized}
    if needed:
        rows = helper_command("input_boolean/list")
        entries = rows if isinstance(rows, list) else (
            rows.get("items", []) if isinstance(rows, dict) else []
        )
        present = {item.get("id") for item in entries if isinstance(item, dict)}
        if not needed.issubset(present):
            raise MissingControls("Streaming library helpers need installation: " +
                                  ", ".join(sorted(needed - present)))
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
    style = """*{box-sizing:border-box}
    :root{color-scheme:dark;--iron:#000;--batten:#eee9e1;--limestone:#b9afa0;
    --slate:#9a9084;--lantern:#f3b55c;--boxwood:#9dbb97;--signal:#ff927c;
    --hair:rgba(238,233,225,.15);--serif:"Cardo","Iowan Old Style",Georgia,serif;
    --sans:"Fira Sans",system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
    html{min-height:100%;background:var(--iron)}
    body{isolation:isolate;position:relative;margin:0;min-height:100vh;min-height:100dvh;
    padding:clamp(20px,6vw,64px) clamp(16px,4vw,44px);font:16px/1.55 var(--sans);
    color:var(--batten);background:radial-gradient(ellipse at 82% 0%,#10100e 0%,transparent 55%),#000}
    body:before,body:after{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none}
    body:before{background:radial-gradient(ellipse at 16% 78%,rgba(169,193,201,.065),transparent 40%),radial-gradient(ellipse at 80% 12%,rgba(243,181,92,.10),transparent 36%);animation:atmosphere 22s ease-in-out infinite alternate}
    body:after{background:radial-gradient(ellipse at 62% 15%,rgba(243,181,92,.10),transparent 46%),radial-gradient(ellipse at 32% 90%,rgba(194,184,228,.04),transparent 40%);
    animation:lantern-drift 18s ease-in-out infinite alternate}
    main{max-width:860px;margin:0 auto;position:relative;isolation:isolate;overflow:hidden;
    padding:clamp(22px,4vw,44px);border:1px solid var(--hair);border-radius:26px;
    background:linear-gradient(145deg,rgba(11,11,11,.90),rgba(1,1,1,.96) 62%);
    box-shadow:0 28px 100px #0009,inset 0 1px 0 rgba(238,233,225,.09);
    animation:arrival .48s cubic-bezier(.2,.75,.25,1) both}
    main:before{content:"";position:absolute;inset:0 12% auto;height:1px;
    background:linear-gradient(90deg,transparent,var(--lantern),transparent);opacity:.65;pointer-events:none;animation:edge-breathe 7s ease-in-out infinite alternate}
    .brand{display:flex;align-items:center;gap:20px;margin-bottom:26px;padding-bottom:24px;
    border-bottom:1px solid var(--hair)}
    .mark{position:relative;flex:0 0 68px;width:68px;height:68px;display:grid;place-items:center;
    border-radius:50%;color:var(--lantern);background:radial-gradient(circle,#15110c,#030303 72%);
    border:1px solid rgba(243,181,92,.48);box-shadow:0 0 38px rgba(243,181,92,.12);
    font:400 33px/1 var(--serif);letter-spacing:-.06em}
    .mark:before,.mark:after{content:"";position:absolute;border-radius:50%;pointer-events:none}
    .mark:before{inset:5px;border:1px solid rgba(243,181,92,.23)}
    .mark:after{inset:-5px;border:1px solid transparent;border-top-color:var(--lantern);
    border-right-color:rgba(243,181,92,.17);animation:orbit 18s linear infinite}
    .brand>div:last-child{min-width:0}
    .eyebrow{margin:0 0 5px;color:var(--lantern);letter-spacing:.19em;font-size:10px;font-weight:650}
    h1{font:400 clamp(29px,5vw,44px)/1.08 var(--serif);margin:0;color:var(--batten);
    letter-spacing:-.025em;overflow-wrap:anywhere}
    h2{font:400 21px/1.3 var(--serif);color:var(--batten);margin:0 0 12px}
    p{margin:12px 0;color:var(--limestone);overflow-wrap:anywhere}
    .muted{color:var(--slate);font-size:13px;line-height:1.55}
    strong{color:var(--batten);font-weight:650}
    .status{display:inline-flex;align-items:center;gap:7px;border:1px solid rgba(157,187,151,.25);
    color:var(--boxwood);background:rgba(157,187,151,.06);border-radius:99px;padding:5px 10px;
    font-size:10px;font-weight:700;letter-spacing:.12em}
    .status:before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor;
    box-shadow:0 0 10px currentColor}
    .module{padding:18px 20px;margin:16px 0;border-radius:16px;border:1px solid var(--hair);
    background:linear-gradient(145deg,rgba(238,233,225,.055),rgba(238,233,225,.018))}
    .module.good{border-left:2px solid var(--boxwood)}
    .module.good p:last-child{margin-bottom:0}
    .module.alert{border-color:rgba(255,146,124,.4);border-left:2px solid var(--signal)}
    .module.alert .status{color:var(--signal);border-color:rgba(255,146,124,.3);background:rgba(255,146,124,.06)}
    .actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:18px 0}
    .actions:has(> :only-child){grid-template-columns:1fr}
    form{margin:0;min-width:0}.actions form{display:block}
    .control,button{display:flex;align-items:center;justify-content:center;text-align:center;
    position:relative;gap:12px;min-height:64px;width:100%;padding:16px 14px;border-radius:12px;
    text-decoration:none;font:600 15px/1.35 var(--sans);letter-spacing:.01em;cursor:pointer;
    border:1px solid var(--hair);background:rgba(238,233,225,.055);color:var(--batten);
    box-shadow:inset 0 1px 0 rgba(238,233,225,.07);overflow-wrap:anywhere;
    transition:background .2s,border-color .2s,box-shadow .2s,transform .16s}
    .control:before,button:before{content:"";width:8px;height:8px;border-radius:50%;
    flex:0 0 8px;background:var(--lantern);box-shadow:0 0 12px rgba(243,181,92,.45)}
    .primary{background:linear-gradient(130deg,#16120d,#080807 80%);
    border-color:rgba(243,181,92,.42);color:#f6ead7}
    .secondary{background:linear-gradient(130deg,#151515,#070707);border-color:rgba(238,233,225,.19)}
    .secondary:before{background:var(--limestone);box-shadow:none}
    .danger{background:linear-gradient(130deg,#1b100e,#080605);border-color:rgba(255,146,124,.46);color:#ffe1d8}
    .danger:before{background:var(--signal);box-shadow:0 0 12px rgba(255,146,124,.25)}
    @media(hover:hover){.control:hover,button:not(:disabled):hover{transform:translateY(-2px);
    background:#211b12;border-color:var(--lantern);box-shadow:0 9px 26px #0006}
    .danger:not(:disabled):hover{background:#2b1814;border-color:var(--signal)}}
    .control:active,button:not(:disabled):active{transform:scale(.985)}
    .control:focus-visible,button:focus-visible,summary:focus-visible{outline:2px solid var(--lantern);outline-offset:4px}
    button:disabled{opacity:.42;cursor:not-allowed;box-shadow:none}
    button:disabled:before{box-shadow:none}
    .module input{color:inherit}
    details{margin:17px 0;border:1px solid var(--hair);border-radius:12px;background:#080808}
    summary{padding:16px 18px;cursor:pointer;color:var(--lantern);font-weight:600}
    details[open] summary{border-bottom:1px solid var(--hair)}details pre{margin:12px}
    pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:55vh;overflow:auto;
    font:12px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;background:#000;
    color:#ded5c6;border:1px solid var(--hair);padding:16px;border-radius:12px;tab-size:2}
    code{color:#f0c990}a:not(.control){color:var(--lantern)}
    @keyframes arrival{from{opacity:0;transform:translateY(9px)}to{opacity:1;transform:translateY(0)}}
    @keyframes orbit{to{transform:rotate(360deg)}}
    @keyframes lantern-drift{from{opacity:.3;transform:translate3d(-3%,0,0) scale(1)}to{opacity:.9;transform:translate3d(4%,2%,0) scale(1.14)}}
    @keyframes atmosphere{from{opacity:.3;transform:translate3d(-4%,2%,0) scale(1.04)}to{opacity:.85;transform:translate3d(5%,-2%,0) scale(1.16)}}
    @keyframes edge-breathe{from{opacity:.25}to{opacity:.85}}
    @keyframes control-arrival{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
    .actions>*{animation:control-arrival .46s ease-out backwards}
    .actions>*:nth-child(2){animation-delay:65ms}.actions>*:nth-child(3){animation-delay:130ms}.actions>*:nth-child(4){animation-delay:195ms}
    @media(max-width:520px){.actions{grid-template-columns:1fr;gap:10px}.brand{gap:15px;padding-bottom:19px;margin-bottom:19px}
    .mark{width:54px;height:54px;flex-basis:54px;font-size:28px}
    .control,button{min-height:58px}.module{padding:15px 16px}}
    @media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important;scroll-behavior:auto!important}}
    """
    body = ('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta charset="utf-8"><title>' + html.escape(title) + '</title>'
            '<style>' + style + '</style></head><body><main><header class="brand">'
            '<div class="mark">H</div><div><p class="eyebrow">HORUS / FULLER FAMILY</p><h1>' +
            html.escape(title) + '</h1></div></header>' + content + '</main></body></html>')
    return body.encode("utf-8")


def form_button(action, label, extra="", danger=False):
    if action not in ("/sync", "/apply", "/rollback", "/setup-household", "/recover-assets", "/schlage-enable", "/schlage-disable", "/watchdog-enable", "/watchdog-disable"):
        raise ValueError("Invalid form action")
    return ('<form action="' + html.escape(action.lstrip("/"), quote=True) + '" method="post">'
            '<input type="hidden" name="csrf" value="' + html.escape(CSRF, quote=True) + '">'
            + extra + '<button class="control ' + ("danger" if danger else "primary") +
            '" type="submit">' + html.escape(label) + '</button></form>')


def action_link(path, label, secondary=False):
    if path not in ("test", "preview", "full-diff", "gallery", "./"):
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
            info = {"status": "running", "mode": "single-dashboard", "target": PATH, "version": "0.3.13"}
            self.respond(200, json.dumps(info).encode("utf-8"), "application/json")
        elif url.path == "/test":
            try:
                current = read_dashboard()
                content = ('<div class="module good"><span class="status">CONNECTED</span>'
                           '<p><strong>FULLER FAMILY</strong></p>'
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
                       '<p><strong>FULLER FAMILY</strong></p></div>')
            if not enabled:
                content += ('<div class="module alert"><h2>Connect Private GitHub</h2>'
                            '<p>Set the GitHub token under the add-on Configuration tab, save, '
                            'then restart the add-on.</p></div>')
                content += '<div class="actions">' + action_link("test", "Test connection") + '</div>'
            else:
                if assets.pending():
                    content += ('<div class="module alert"><h2>Interrupted update</h2><p>Recover the verified previous dashboard and assets before applying another update.</p>' + form_button("/recover-assets", "Recover interrupted update", danger=True) + "</div>")
                can_restore = bool(state.get("backup") and not state.get("rolled_back") and not assets.pending())
                restore = (
                    form_button("/rollback", "Restore previous dashboard", danger=True)
                    if can_restore else
                    '<button type="button" class="danger" disabled>Restore previous dashboard</button>'
                )
                content += ('<div class="actions">'
                            + form_button("/sync", "Sync dashboard to GitHub")
                            + action_link("preview", "Preview proposed update")
                            + action_link("test", "Test connection", secondary=True)
                            + restore + '</div>')
            content += '<div class="actions">' + action_link("gallery", "Install Living Gallery artwork", secondary=True) + "</div>"
            content += ('<div class="module"><h2>Schlage fast reporting trial</h2><p>Request a shared account refresh every 10 seconds. Native polling stays enabled. No lock or unlock commands. If cloud reporting fails, disable this trial.</p><div class="actions">' + form_button("/schlage-enable", "Approve &amp; Enable 10-second polling".replace("&amp;", "&")) + form_button("/schlage-disable", "Disable fast polling", danger=True) + "</div></div>")
            with WATCHDOG_STATE_LOCK:
                wd = dict(WATCHDOG_STATUS)
            content += ('<div class="module"><h2>HORUS Kiosk Health</h2><p>Five-second health checks. Status: ' + html.escape(str(wd["phase"])) + ' | Recovery: ' + ("enabled" if watchdog_enabled() else "disabled") + '</p><p>Last heartbeat: ' + html.escape(str(wd["last_heartbeat"] or "not received")) + '<br>Last recovery: ' + html.escape(str(wd["last_recovery"] or "none")) + '<br>Recovery attempts this process: ' + str(wd["recoveries"]) + '</p><div class="actions">' + form_button("/watchdog-enable","Approve & Enable kiosk recovery") + form_button("/watchdog-disable","Disable kiosk recovery",danger=True) + "</div></div>")
            if enabled:
                content += ('<div class="module"><h2>Nanit Insights candidate</h2><p>Stage the pinned read-only companion in Core configuration. This does not restart Core or change the dashboard. Configuration validation and a separately approved restart are required.</p><div class="actions">' + form_button("/nanit-stage", "Approve & Stage Nanit companion") + "</div></div>")
            content += ('<p class="muted">Only Fuller Family can be changed. '
                        'A proposal requires your approval before saving.</p>')
            self.show(200, "Dashboard Manager", content)
        elif url.path == "/gallery":
            try:
                verified = gallery_import.count()
                self.show(200, "Private Living Gallery", "<p>Verified artwork: " + str(verified) + " / 5. Upload the approved Collection 001 ZIP. Images stay on your HA Green.</p>" + '<form method="post" action="gallery-import" enctype="multipart/form-data"><input type="hidden" name="csrf" value="' + html.escape(CSRF, quote=True) + '"><input required type="file" name="artwork" accept=".zip"><button type="submit">Install Collection 001</button></form><div class="actions">' + action_link("./", "Back") + "</div>")
            except assets.AssetError as exc:
                self.show(409, "Gallery unavailable", "<p>" + html.escape(str(exc)) + "</p>")
        elif url.path == "/preview":
            with LOCK:
                try:
                    proposed, sha = load_proposal()
                    live = read_dashboard()
                    require_fresh(proposed, live)
                    asset_hash, asset_text = asset_bridge.prepare(proposed, live)
                    asset_display = ("<div class=\'module\'><h2>Verified assets requested</h2><p>Frontend modules run in Home Assistant browsers and resources are global. Approve only reviewed code.</p><pre>" + html.escape(asset_text) + "</pre></div>") if asset_text else ""
                    display_diff = compare_dashboard_configs(live, proposed["config"], compact=True)
                    if len(display_diff) > 240_000:
                        raise BridgeError("Compact preview is too large; narrow the proposed update")
                    extra = ('<input type="hidden" name="proposal_sha" value="' +
                             html.escape(sha, quote=True) + '">')
                    extra += '<input type="hidden" name="asset_hash" value="' + html.escape(asset_hash, quote=True) + '">'
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
                               asset_display + '<div class="actions">' + form_button("/apply", "Approve & Apply", extra) + '</div>' +
                               '<div class="actions">' + action_link("./", "Cancel", secondary=True) + '</div>')
                    self.show(200, "Review Fuller Family update", content)
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
        if route.path == "/gallery-import" and not route.query and not route.fragment:
            try:
                size = int(self.headers.get("Content-Length","0"))
                if not 1 <= size <= gallery_import.MAX_BYTES + 8192:
                    raise assets.AssetError("Invalid artwork upload size")
                self.connection.settimeout(30)
                raw = gallery_import.parse_form(self.headers.get("Content-Type",""), self.rfile.read(size), CSRF)
                with LOCK:
                    count = gallery_import.install(raw)
                self.show(200,"Collection installed","<p>Verified " + str(count) + " / 5 paintings locally. Preview the gallery dashboard update.</p><div class='actions'>" + action_link("preview","Preview dashboard update") + "</div>")
            except Exception as exc:
                detail = str(exc) if isinstance(exc, assets.AssetError) else "Artwork import failed"
                self.show(409,"Gallery import failed","<p>"+html.escape(detail)+"</p><div class='actions'>"+action_link("gallery","Back")+"</div>")
            return
        if route.path not in ("/sync", "/apply", "/rollback", "/setup-household", "/recover-assets", "/schlage-enable", "/schlage-disable", "/watchdog-enable", "/watchdog-disable", "/nanit-stage") or route.query:
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
                elif route.path == "/nanit-stage":
                    status = nanit_install.stage(github_request)
                    title = "Nanit companion staged"
                    message = "<p>" + html.escape(status) + "</p>"
                elif route.path == "/setup-household":
                    count = install_family_controls()
                    title = "Family Controls Ready"
                    message = ("<p>Heru, Nest, humidity, and room AC controls are installed (" + str(count) +
                               " fixed scripts).</p><div class='actions'>" +
                               action_link("preview", "Preview Dashboard Update") + "</div>")
                elif route.path == "/recover-assets":
                    count = recover_assets()
                    title = "Interrupted update recovered"
                    message = "<p>Verified previous dashboard and assets restored. Views: " + str(count) + ".</p>"
                elif route.path in ("/schlage-enable", "/schlage-disable"):
                    status = set_schlage_polling(route.path == "/schlage-enable")
                    title = "Schlage polling trial"
                    message = "<p>Verified automation state: " + html.escape(status) + ". Default Schlage polling remains available. No door controls changed.</p>"
                elif route.path in ("/watchdog-enable", "/watchdog-disable"):
                    enabled = route.path == "/watchdog-enable"
                    set_watchdog(enabled)
                    title = "Kiosk watchdog " + ("enabled" if enabled else "disabled")
                    message = "<p>Camera recovery stays in the dashboard. Full kiosk recovery is " + ("enabled" if enabled else "disabled") + ".</p>"
                elif route.path == "/apply":
                    sha = data.get("proposal_sha", [""])[0]
                    result = apply_proposal(sha, data.get("asset_hash", [""])[0])
                    title = "Dashboard updated"
                    message = ("<p>Fuller Family saved and verified with " +
                               str(result["views"]) + " view(s). Automatic backup created.</p>")
                    if refresh_kiosk_after_apply():
                        message += "<p>Kiosk cache clear, blank-page transition and dashboard reload accepted by Home Assistant.</p>"
                    else:
                        message += "<p>Dashboard saved; kiosk refresh incomplete after retry. Check the tablet and reopen the dashboard if needed.</p>"
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
            detail = str(exc) if isinstance(exc, (BridgeError, assets.AssetError)) else "Unexpected operation failure; inspect add-on logs"
            self.show(409, "Action failed", "<p>" + html.escape(detail) +
                      '</p><div class="actions">' + action_link("./", "Dashboard Manager", secondary=True) + '</div>')

    def do_PUT(self):
        self.show(405, "Not allowed", "<p>Only approved UI actions are supported.</p>")

    do_PATCH = do_PUT
    do_DELETE = do_PUT


QUIET_EXIT_ID = "horus_weekday_quiet_exit_0531"
QUIET_EXIT_MARKER = "/data/horus_weekday_quiet_exit_0531.installed"
QUIET_EXIT_VOICE = "event.bedroom_master_bdrm_spot_voice_event"


def quiet_exit_config():
    return {
        "alias": "HORUS Weekday Quiet Exit",
        "description": "Owner-approved: after the final 05:31 alarm, respond to a reported alarm snooze or dismissal only while Heru sleep mode is on.",
        "mode": "single",
        "triggers": [{"trigger": "state", "entity_id": QUIET_EXIT_VOICE, "to": None}],
        "conditions": [
            {"condition": "time", "after": "05:31:00", "before": "06:30:00",
             "weekday": ["mon", "tue", "wed", "thu", "fri"]},
            {"condition": "state", "entity_id": SLEEP_HELPER, "state": "on"},
            {"condition": "template", "value_template": """{% set v = state_attr('media_player.bedroom_master_bdrm_spot', 'volume_level') %}{{ is_number(v) and v | float > 0.1 }}"""},
            {"condition": "template", "value_template": """{% if trigger is defined and trigger.from_state is not none and trigger.to_state is not none and trigger.from_state.state != trigger.to_state.state and trigger.to_state.state not in ['unknown','unavailable'] %}
{% set a = trigger.to_state.attributes %}
{% set cmd = (a.get('voice_command') or '') | string | lower | trim | regex_replace('^alexa[ ,]+', '') | regex_replace('[.!?]+$', '') | trim %}
{% set intent = (a.get('intent') or '') | string | lower %}
{{ cmd in ['snooze','snooze alarm','snooze the alarm','snooze my alarm','stop alarm','stop the alarm','stop my alarm','dismiss alarm','dismiss the alarm','dismiss my alarm'] or (cmd == 'stop' and 'alarm' in intent) }}
{% else %}false{% endif %}"""}
        ],
        "actions": [{"action": "media_player.volume_set", "target": {"entity_id": SPOT},
                     "data": {"volume_level": 0.1}}],
    }


def install_quiet_exit_once():
    """Create one fixed owner-requested automation; never overwrite or re-create a removed one."""
    if os.path.exists(QUIET_EXIT_MARKER):
        return
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return
    url = "http://supervisor/core/api/config/automation/config/" + QUIET_EXIT_ID
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    try:
        try:
            with urlopen(Request(url, headers=headers), timeout=15) as response:
                existing = json.loads(response.read(100000))
            if not isinstance(existing, dict):
                raise BridgeError("Unexpected automation config")
            # Preserve any existing automation with this ID, including owner edits.
        except HTTPError as exc:
            if exc.code != 404:
                raise
            req = Request(url, headers=headers, method="POST",
                          data=json.dumps(quiet_exit_config()).encode("utf-8"))
            with urlopen(req, timeout=20) as response:
                response.read(100000)
            with urlopen(Request(url, headers=headers), timeout=15) as response:
                verified = json.loads(response.read(100000))
            if verified.get("alias") != quiet_exit_config()["alias"]:
                raise BridgeError("Quiet exit verification failed")
        with open(QUIET_EXIT_MARKER, "w", encoding="utf-8") as marker:
            marker.write(QUIET_EXIT_ID)
        LOG.info("Owner-approved weekday quiet exit automation present")
    except Exception as exc:
        LOG.warning("Weekday quiet exit installation not verified: %s", type(exc).__name__)


DOORBELL_SCRIPT_ID = "horus_doorbell_live_view"
DOORBELL_MARKER = "/data/horus_doorbell_live_view.inplace.installed"
DOORBELL_OLD_MARKER = "/data/horus_doorbell_live_view.installed"


def previous_doorbell_config():
    return {"alias":"HORUS Doorbell Live View","mode":"single","max_exceeded":"silent","sequence":[{"variables":{"current_page":"{{ states('sensor.entryway_fuller_family_kiosk_current_page') }}","dashboard_prefix":"http://192.168.1.208/fuller-family-test/","doorbell_url":"http://192.168.1.208/fuller-family-test/camera-blink-doorbell-live?horus_kiosk=1&hide_sidebar&kiosk"}},{"condition":"template","value_template":"{{ current_page.startswith(dashboard_prefix) and current_page.split('?')[0] != doorbell_url.split('?')[0] }}"},{"if":[{"condition":"state","entity_id":"switch.entryway_fuller_family_kiosk_screen","state":"off"}],"then":[{"action":"switch.turn_on","target":{"entity_id":"switch.entryway_fuller_family_kiosk_screen"}}]},{"action":"fully_kiosk.load_url","data":{"device_id":"e8969f67449f17aa9e2c94db1e099018","url":"{{ doorbell_url }}"}},{"delay":"00:00:05"}]}


def install_doorbell_once():
    """Preserve the Alexa entity ID; migrate only the exact prior fixed script."""
    if os.path.exists(DOORBELL_MARKER):
        return
    try:
        current = script_request(DOORBELL_SCRIPT_ID)
        expected = preset_scripts()[DOORBELL_SCRIPT_ID]
        actual = current.get("config", current) if isinstance(current, dict) else current
        if current is None:
            if os.path.exists(DOORBELL_OLD_MARKER):
                return  # Owner removed it; do not recreate.
            script_request(DOORBELL_SCRIPT_ID, "POST", expected)
        elif actual == previous_doorbell_config():
            script_request(DOORBELL_SCRIPT_ID, "POST", expected)
        elif actual != expected:
            raise BridgeError("Existing doorbell script differs; preserved")
        verified = script_request(DOORBELL_SCRIPT_ID)
        actual = verified.get("config", verified) if isinstance(verified, dict) else None
        if actual != expected:
            raise BridgeError("Doorbell migration not verified")
        with open(DOORBELL_MARKER, "w", encoding="utf-8") as marker:
            marker.write(DOORBELL_SCRIPT_ID)
    except Exception as exc:
        LOG.warning("Doorbell script installation not verified: %s", type(exc).__name__)


if __name__ == "__main__":
    LOG.setLevel(logging.INFO)
    install_quiet_exit_once()
    install_doorbell_once()
    gallery_curator.start()
    threading.Thread(target=watchdog_worker,name='horus-kiosk-watchdog',daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8099), Handler).serve_forever()
