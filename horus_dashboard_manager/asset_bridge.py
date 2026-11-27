"""Narrow Home Assistant resource adapter."""
import json
import os
import asset_manager as assets

def resource(operation, *, name=None, resource_id=None, url=None, res_type=None):
    if operation == "list":
        if any(v is not None for v in (name, resource_id, url, res_type)):
            raise assets.AssetError("Invalid resource listing")
        args = {}
    else:
        if operation not in ("create", "update", "delete"):
            raise assets.AssetError("Unsupported resource operation")
        path = "/local/horus/" + assets.relative(name)
        if operation in ("create", "update") and (
            not isinstance(url, str) or url.split("?", 1)[0] != path or res_type not in ("module", "css", "js")
        ):
            raise assets.AssetError("Resource outside approved HORUS path")
        current = assets.resource_for(resource("list"), name)
        if operation == "create":
            if current is not None or resource_id is not None:
                raise assets.AssetError("Resource already exists")
            args = {"url": url, "res_type": res_type}
        else:
            if current is None or current["id"] != resource_id:
                raise assets.AssetError("Resource identity changed")
            args = {"resource_id": resource_id}
            if operation == "update":
                args.update(url=url, res_type=res_type)
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise assets.AssetError("Supervisor token unavailable")
    from websockets.sync.client import connect
    with connect("ws://supervisor/core/websocket", open_timeout=8, close_timeout=3,
                 max_size=1200000) as ws:
        if json.loads(ws.recv(timeout=8)).get("type") != "auth_required":
            raise assets.AssetError("Unexpected resource authentication challenge")
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(ws.recv(timeout=8)).get("type") != "auth_ok":
            raise assets.AssetError("Resource authentication rejected")
        ws.send(json.dumps({"id": 1, "type": "lovelace/resources/" + operation, **args}))
        result = json.loads(ws.recv(timeout=20))
        if result.get("type") != "result" or result.get("id") != 1 or not result.get("success"):
            raise assets.AssetError("Home Assistant rejected resource operation; storage mode required")
        return result.get("result")

def managed_cards(node):
    found = set()
    if isinstance(node, dict):
        t = node.get("type")
        if isinstance(t, str) and t.startswith("custom:horus-"):
            found.add(t.removeprefix("custom:") + ".js")
        for child in node.values():
            found |= managed_cards(child)
    elif isinstance(node, list):
        for child in node:
            found |= managed_cards(child)
    return found

def prepare(proposal, live):
    if assets.pending():
        raise assets.AssetError("Unfinished asset transaction. Recovery required.")
    prior = {v.get("path"): v for v in live["views"]}
    changed = [v for v in proposal["config"]["views"] if prior.get(v.get("path")) != v]
    m = proposal.get("assets")
    declared = {i["path"] for i in assets.manifest(m)["files"] if i["register"]} if m is not None else set()
    for name in managed_cards(changed) - declared:
        data = assets.read(name)
        row = assets.resource_for(resource("list"), name)
        if data is None or row is None or row["type"] != "module" or row["url"] != "/local/horus/" + name + "?v=" + assets.digest(data)[:16]:
            raise assets.AssetError("Required verified HORUS module is missing: " + name)
    if m is None:
        return "", ""
    entries = assets.plan(m, resource)
    lines = ["Git commit " + m["ref"]]
    for e in entries:
        i = e["item"]
        lines.append(i["path"] + " | " + str(i["size"]) + " bytes | SHA256 " + i["sha256"]
                     + (" | global Lovelace resource" if i["register"] else ""))
    return assets.digest(assets.canonical(entries)), "\n".join(lines)
