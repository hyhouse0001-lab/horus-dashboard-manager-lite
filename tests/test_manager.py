import base64
import json
import pathlib
import sys
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "horus_dashboard_manager"))
import manager


class ScopeTests(unittest.TestCase):
    def test_sync_replaces_snapshot_without_inline_content(self):
        document = {"config": {"views": [{"title": "Café"}]}}
        metadata = {"encoding": "none", "content": "", "sha": "existing"}
        with patch.object(manager, "github_request", side_effect=[metadata, {}]) as request:
            manager.github_put(manager.SNAPSHOT_PATH, document, "Sync")
        path, method, payload = request.call_args.args
        self.assertEqual((path, method), (manager.SNAPSHOT_PATH, "PUT"))
        self.assertEqual(payload["sha"], "existing")
        raw = base64.b64decode(payload["content"]).decode()
        self.assertEqual(json.loads(raw), document)
        self.assertEqual(raw.count("\n"), 1)

    def test_single_dashboard_path(self):
        self.assertEqual(manager.PATH, "fuller-family-test")

    def test_wrong_target_rejected_before_token_check(self):
        with self.assertRaises(manager.BridgeError):
            manager.ha_call("lovelace/config/save", url_path="other-dashboard", config={"views":[{"title":"x"}]})

    def test_disallowed_commands_rejected(self):
        with self.assertRaises(manager.BridgeError):
            manager.ha_call("lock/unlock", entity_id="lock.front_door")

    def test_empty_dashboard_rejected(self):
        with self.assertRaises(manager.BridgeError):
            manager.check_config({"views":[]})

    def test_malformed_dashboard_rejected(self):
        with self.assertRaises(manager.BridgeError):
            manager.check_config({"title":"Missing views"})

    def test_confirmed_exact_front_door_unlock_is_allowed(self):
        action = {
            "action": "perform-action",
            "perform_action": "lock.unlock",
            "target": {"entity_id": "lock.front_of_house_front_door"},
            "confirmation": {"text": "Unlock the front door?"}
        }
        config = {"views": [{"title": "Home", "cards": [{"type": "button", "tap_action": action}]}]}
        self.assertEqual(manager.check_config(config), config)

    def test_confirmed_exact_back_door_unlock_is_allowed(self):
        action = {
            "action": "perform-action",
            "perform_action": "lock.unlock",
            "target": {"entity_id": "lock.back_door"},
            "confirmation": {"text": "Unlock the back door?"}
        }
        config = {"views": [{"title": "Security", "cards": [{"type": "button", "tap_action": action}]}]}
        self.assertEqual(manager.check_config(config), config)

    def test_back_door_unlock_without_confirmation_rejected(self):
        action = {
            "action": "perform-action",
            "perform_action": "lock.unlock",
            "target": {"entity_id": "lock.back_door"}
        }
        with self.assertRaises(manager.BridgeError):
            manager.check_config({"views": [{"cards": [{"tap_action": action}]}]})

    def test_both_door_unlocks_with_confirmation_allowed(self):
        actions = [
            {"action": "perform-action", "perform_action": "lock.unlock",
             "target": {"entity_id": entity},
             "confirmation": {"text": "Confirm unlocking this door?"}}
            for entity in ("lock.front_of_house_front_door", "lock.back_door")
        ]
        config = {"views": [{"cards": [{"tap_action": action} for action in actions]}]}
        self.assertEqual(manager.check_config(config), config)

    def test_other_lock_or_multi_target_unlock_rejected(self):
        for target in (
            {"entity_id": "lock.other_door"},
            {"entity_id": ["lock.front_of_house_front_door", "lock.back_door"]},
            {"area_id": "house"},
            {"entity_id": "all"},
        ):
            action = {"action": "perform-action", "perform_action": "lock.unlock",
                      "target": target, "confirmation": {"text": "Confirm unlock?"}}
            with self.subTest(target=target), self.assertRaises(manager.BridgeError):
                manager.check_config({"views": [{"cards": [{"tap_action": action}]}]})

    def test_blank_confirmation_and_legacy_service_unlock_rejected(self):
        for entity in ("lock.front_of_house_front_door", "lock.back_door"):
            for confirmation in (None, {}, {"text": "   "}):
                action = {"action": "perform-action", "perform_action": "lock.unlock",
                          "target": {"entity_id": entity}, "confirmation": confirmation}
                with self.subTest(entity=entity, confirmation=confirmation), self.assertRaises(manager.BridgeError):
                    manager.check_config({"views": [{"cards": [{"tap_action": action}]}]})
            legacy = {"action": "call-service", "service": "lock.unlock",
                      "target": {"entity_id": entity}, "confirmation": {"text": "Unlock?"}}
            with self.assertRaises(manager.BridgeError):
                manager.check_config({"views": [{"cards": [{"tap_action": legacy}]}]})

    def test_streaming_helper_create_allowlist(self):
        for name in ("HORUS Living Room Streaming", "HORUS Bedroom Streaming"):
            with patch.object(manager.os.environ, "get", return_value=None):
                with self.assertRaisesRegex(manager.BridgeError, "Supervisor token unavailable"):
                    manager.helper_command("input_boolean/create", name=name,
                                           icon="mdi:television-play")
        with self.assertRaisesRegex(manager.BridgeError, "Unapproved helper create"):
            manager.helper_command("input_boolean/create", name="Other streaming",
                                   icon="mdi:television-play")

    def test_missing_streaming_helper_prevents_preview(self):
        config = {"views": [{"cards": [{"type": "conditional",
                  "conditions": [{"entity": "input_boolean.horus_bedroom_streaming",
                                  "state": "on"}]}]}]}
        with patch.object(manager, "helper_command", return_value=[]):
            with self.assertRaises(manager.MissingControls):
                manager.validate_required_controls(config)

    def test_unlock_without_confirmation_rejected(self):
        action = {
            "action": "perform-action",
            "perform_action": "lock.unlock",
            "target": {"entity_id": "lock.front_of_house_front_door"},
        }
        with self.assertRaises(manager.BridgeError):
            manager.check_config({"views": [{"cards": [{"tap_action": action}]}]})

    def test_unlock_of_any_other_lock_rejected(self):
        action = {
            "action": "perform-action",
            "perform_action": "lock.unlock",
            "target": {"entity_id": "lock.other_door"},
            "confirmation": {"text": "Unlock?"}
        }
        with self.assertRaises(manager.BridgeError):
            manager.check_config({"views": [{"cards": [{"tap_action": action}]}]})

    def test_direct_lock_unlock_shortcut_rejected(self):
        config = {"views":[{"cards":[{"tap_action":{"action":"call-service","service":"lock.unlock"}}]}]}
        with self.assertRaises(manager.BridgeError):
            manager.check_config(config)

    def test_disarm_shortcut_rejected(self):
        config = {"views":[{"cards":[{"tap_action":{"perform_action":"alarm_control_panel.alarm_disarm"}}]}]}
        with self.assertRaises(manager.BridgeError):
            manager.check_config(config)

    def test_valid_dashboard_accepted(self):
        config = {"title":"Fuller Family", "views":[{"title":"Home","cards":[{"type":"entities","entities":["sensor.a"]}]}]}
        self.assertEqual(manager.check_config(config), config)

    def test_stale_proposal_rejected(self):
        live = {"views":[{"title":"Home"}]}
        proposal = {"base_sha256":"0"*64,"config":{"views":[{"title":"Other"}]}}
        with self.assertRaises(manager.BridgeError):
            manager.require_fresh(proposal, live)

    def test_noop_proposal_rejected(self):
        live = {"views":[{"title":"Home"}]}
        proposal = {"base_sha256":manager.digest(live),"config":live}
        with self.assertRaises(manager.BridgeError):
            manager.require_fresh(proposal, live)

    def test_valid_proposal_accepted(self):
        live = {"views":[{"title":"Home"}]}
        proposal = {"base_sha256":manager.digest(live),"config":{"views":[{"title":"Changed"}]}}
        manager.require_fresh(proposal, live)


    def test_form_actions_stay_inside_home_assistant_ingress(self):
        for action in ("/sync", "/apply", "/rollback"):
            markup = manager.form_button(action, "Action")
            self.assertIn('action="' + action.lstrip("/") + '"', markup)
            self.assertNotIn('action="/', markup)


    def test_sidebar_uses_short_name_and_panel_title(self):
        manifest = (ROOT / "horus_dashboard_manager" / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("name: Horus DBM", manifest)
        self.assertIn("panel_title: Horus DBM", manifest)
        self.assertIn("version: 0.3.3", manifest)

    def test_manager_links_are_styled_buttons(self):
        preview = manager.action_link("preview", "Preview")
        test = manager.action_link("test", "Test")
        self.assertIn('class="control primary"', preview)
        self.assertIn('class="control primary"', test)
        self.assertIn('href="preview"', preview)
        self.assertNotIn('href="/preview"', preview)
        html_content = manager.page("Dashboard Manager", preview).decode("utf-8")
        self.assertIn("HORUS / FULLER FAMILY", html_content)
        self.assertIn("radial-gradient", html_content)

    def test_manager_still_rejects_unlisted_navigation(self):
        with self.assertRaises(ValueError):
            manager.action_link("https://example.com", "External")
        with self.assertRaises(ValueError):
            manager.form_button("/unlock", "Unlock")

    def test_private_repo_and_paths(self):
        self.assertEqual(manager.PRIVATE_REPO, "hyhouse0001-lab/horus-home-os")
        self.assertEqual(manager.SNAPSHOT_PATH, "dashboard_sync/fuller-family-test.current.json")
        self.assertEqual(manager.PROPOSAL_PATH, "dashboard_sync/fuller-family-test.proposal.json")


class DeploymentTests(unittest.TestCase):
    def test_happy_path_requires_matching_review_and_backs_up(self):
        before = {"views": [{"title": "Before"}]}
        after = {"views": [{"title": "After"}]}
        candidate = {
            "schema_version": 1, "dashboard": manager.PATH,
            "base_sha256": manager.digest(before),
            "summary": "Change the view title", "config": after,
        }
        live = [before]
        def read():
            return live[0]
        def save(value):
            live[0] = value
        with patch.object(manager, "load_proposal", return_value=(candidate, "abc123")), \
             patch.object(manager, "read_dashboard", side_effect=read), \
             patch.object(manager, "save_dashboard", side_effect=save), \
             patch.object(manager, "backup", return_value="backup.json") as backup, \
             patch.object(manager, "state_write") as state_write, \
             patch.object(manager, "github_put") as github_put:
            result = manager.apply_proposal("abc123")
        backup.assert_called_once_with(before)
        self.assertEqual(result["views"], 1)
        self.assertEqual(live[0], after)
        self.assertTrue(result["synced"])
        self.assertEqual(state_write.call_args.args[0]["before_sha256"], manager.digest(before))
        self.assertEqual(state_write.call_args.args[0]["after_sha256"], manager.digest(after))
        github_put.assert_called_once()

    def test_changed_proposal_never_saves(self):
        before = {"views": [{"title": "Before"}]}
        candidate = {
            "schema_version": 1, "dashboard": manager.PATH,
            "base_sha256": manager.digest(before),
            "summary": "Change view", "config": {"views": [{"title": "After"}]},
        }
        with patch.object(manager, "load_proposal", return_value=(candidate, "new-sha")), \
             patch.object(manager, "read_dashboard") as read, \
             patch.object(manager, "save_dashboard") as save:
            with self.assertRaises(manager.BridgeError):
                manager.apply_proposal("old-sha")
        read.assert_not_called()
        save.assert_not_called()

    def test_external_edit_blocks_rollback(self):
        deployed = {"views": [{"title": "After"}]}
        current = {"views": [{"title": "Changed elsewhere"}]}
        state = {
            "backup": "backup.json", "rolled_back": False,
            "after_sha256": manager.digest(deployed),
        }
        with patch.object(manager, "state_read", return_value=state), \
             patch.object(manager, "read_dashboard", return_value=current), \
             patch.object(manager, "save_dashboard") as save:
            with self.assertRaises(manager.BridgeError):
                manager.rollback_last()
        save.assert_not_called()

    def test_restore_may_recover_existing_lock_button(self):
        previous = {"views": [{"cards": [{"tap_action": {"service": "lock.unlock"}}]}]}
        self.assertEqual(manager.check_config(previous, restoring=True), previous)
        with self.assertRaises(manager.BridgeError):
            manager.check_config(previous)


class FamilyControlsTests(unittest.TestCase):
    def test_presets_only_touch_selected_entities(self):
        p = manager.preset_scripts()
        self.assertEqual(len(p), 19)
        self.assertEqual(p["horus_heru_sleep"]["sequence"][0]["data"]["volume_level"], 0.1)
        self.assertEqual(p["horus_heru_awake"]["sequence"][0]["data"]["volume_level"], 0.7)
        def collect_targets(node):
            if isinstance(node, dict):
                targets = set()
                if "target" in node:
                    targets.add(node["target"]["entity_id"])
                for value in node.values():
                    targets.update(collect_targets(value))
                return targets
            if isinstance(node, list):
                return set().union(*(collect_targets(value) for value in node))
            return set()
        touched = collect_targets(p)
        self.assertEqual(touched, {"media_player.bedroom_master_bdrm_spot",
                                   "input_boolean.horus_heru_sleeping",
                                   "humidifier.basement_dehumidifier",
                                   "climate.nest_learning_thermostat_4th_gen",
                                   "climate.living_room_living_room_ac",
                                   "climate.bedroom_my_bedroom_ac",
                                   "climate.dining_room_dining_room_ac",
                                   "climate.herus_room_herus_ac",
                                   "climate.guest_bedroom_guest_room_ac",
                                   "media_player.living_room_the_tv_in_the_living_room",
                                   "media_player.living_room_the_tv_in_the_living_room_3",
                                   "remote.living_room_the_tv_in_the_living_room"})
        self.assertIn(" | min | int", p["horus_nest_temp_up"]["sequence"][0]["data"]["temperature"])
        self.assertIn(" | max | int", p["horus_nest_temp_down"]["sequence"][0]["data"]["temperature"])
        self.assertEqual(p["horus_nest_temp_up"]["sequence"][0]["action"], "climate.set_temperature")
        self.assertEqual(p["horus_nest_temp_down"]["sequence"][0]["action"], "climate.set_temperature")
    def test_script_id_restriction(self):
        with self.assertRaises(manager.BridgeError):
            manager.script_request("someone_else_script", "POST", {})
    def test_helper_restriction(self):
        with self.assertRaises(manager.BridgeError):
            manager.helper_command("input_boolean/create", name="Other Helper")


class RingAlarmPermissionTests(unittest.TestCase):
    def check(self, action):
        manager.check_config({"views":[{"title":"Home","cards":[{"type":"button","tap_action":action}]}]})

    def test_ring_mqtt_disarm_requires_confirmation(self):
        act={"action":"perform-action","perform_action":"alarm_control_panel.alarm_disarm",
             "target":{"entity_id":"alarm_control_panel.home_alarm"},"confirmation":{"text":"Disarm Ring?"}}
        self.check(act)
        act.pop("confirmation")
        with self.assertRaises(manager.BridgeError):
            self.check(act)

    def test_arm_home_and_away_only_ring(self):
        for name in ("alarm_control_panel.alarm_arm_home","alarm_control_panel.alarm_arm_away"):
            valid={"action":"perform-action","perform_action":name,
                   "target":{"entity_id":"alarm_control_panel.home_alarm"}}
            self.check(valid)
            valid["target"]={"entity_id":"alarm_control_panel.some_other_alarm"}
            with self.assertRaises(manager.BridgeError):
                self.check(valid)

    def test_disarm_cannot_target_other_alarm(self):
        bad={"action":"perform-action","perform_action":"alarm_control_panel.alarm_disarm",
             "target":{"entity_id":"alarm_control_panel.blink_home"},
             "confirmation":{"text":"Disarm?"}}
        with self.assertRaises(manager.BridgeError):
            self.check(bad)



class PreviewDiffTests(unittest.TestCase):
    def test_large_svg_has_readable_preview_and_full_exact_diff(self):
        from urllib.parse import quote
        artwork = "<svg xmlns='http://www.w3.org/2000/svg'><text>RING ALARM</text>" + ("x" * 150000) + "</svg>"
        original = "data:image/svg+xml," + quote(artwork)
        updated = original.replace("RING%20ALARM", "RING%20SECURITY")
        before = {"views":[{"title":"Home","cards":[{"type":"picture-elements","image":original}]}]}
        after = {"views":[{"title":"Home","cards":[{"type":"picture-elements","image":updated}]}]}
        compact = manager.compare_dashboard_configs(before, after, compact=True)
        exact = manager.compare_dashboard_configs(before, after, compact=False)
        self.assertIn("SHA256", compact)
        self.assertIn("RING ALARM", compact)
        self.assertIn("RING SECURITY", compact)
        self.assertLess(len(compact), 10000)
        self.assertGreater(len(exact), 110000)
        self.assertIn("data:image/svg+xml,", exact)
        self.assertEqual(before["views"][0]["cards"][0]["image"], original)
        self.assertEqual(after["views"][0]["cards"][0]["image"], updated)
        self.assertIn('href="full-diff"', manager.action_link("full-diff", "Full diff"))

    def test_svg_changes_are_detected_even_when_labels_match(self):
        from urllib.parse import quote
        first = "data:image/svg+xml," + quote('<svg><text>HOME</text><rect fill="blue"/></svg>')
        second = "data:image/svg+xml," + quote('<svg><text>HOME</text><rect fill="red"/></svg>')
        before = {"views":[{"cards":[{"image":first}]}]}
        after = {"views":[{"cards":[{"image":second}]}]}
        compact = manager.compare_dashboard_configs(before, after, compact=True)
        self.assertIn("SHA256", compact)
        self.assertIn("HOME", compact)
        self.assertNotEqual(manager.review_representation(before),
                            manager.review_representation(after))


class ACControlsTests(unittest.TestCase):
    def test_each_frigidaire_unit_has_two_bounded_scripts(self):
        presets = manager.preset_scripts()
        for room, entity in manager.AC_UNITS.items():
            for direction in ("up", "down"):
                name = "horus_ac_" + room + "_" + direction
                self.assertIn(name, presets)
                script = presets[name]
                self.assertEqual(script["sequence"][0]["action"], "climate.set_temperature")
                self.assertEqual(script["sequence"][0]["target"], {"entity_id": entity})
                expr = script["sequence"][0]["data"]["temperature"]
                self.assertIn(" | max] | min | int }}", expr)
                self.assertIn("state_attr('" + entity + "', 'temperature')", expr)
                self.assertIn("+ 1" if direction == "up" else "- 1", expr)

    def test_missing_nest_scripts_are_reported_as_setup_prerequisite(self):
        config = {"views": [{"cards": [
            {"tap_action": {"perform_action": "script.turn_on",
                            "target": {"entity_id": "script.horus_nest_temp_up"}}}]}]}
        with patch.object(manager, "script_request", return_value=None):
            with self.assertRaises(manager.MissingControls) as ctx:
                manager.validate_required_controls(config)
        self.assertIn("horus_nest_temp_up", str(ctx.exception))

    def test_only_referenced_scripts_are_checked(self):
        config = {"views": [{"cards": [
            {"tap_action": {"perform_action": "script.turn_on",
                            "target": {"entity_id": "script.horus_nest_temp_up"}}}]}]}
        preset = manager.preset_scripts()["horus_nest_temp_up"]
        with patch.object(manager, "script_request", return_value=preset) as request:
            manager.validate_required_controls(config)
        request.assert_called_once_with("horus_nest_temp_up")

    def test_unknown_horus_script_rejected(self):
        config = {"views":[{"cards":[{"tap_action":{
            "perform_action":"script.turn_on",
            "target":{"entity_id":"script.horus_unrelated"} }}]}]}
        with self.assertRaises(manager.BridgeError):
            manager.required_preset_ids(config)

    def test_air_conditioner_can_only_use_cool_off_on_approved_entities(self):
        for entity in manager.AC_UNITS.values():
            for mode in ("cool", "off"):
                action={"action":"perform-action","perform_action":"climate.set_hvac_mode",
                        "target":{"entity_id":entity},"data":{"hvac_mode":mode}}
                manager.check_config({"views":[{"cards":[{"tap_action":action}]}]})
            for mode in ("heat","dry"):
                bad={"action":"perform-action","perform_action":"climate.set_hvac_mode",
                     "target":{"entity_id":entity},"data":{"hvac_mode":mode}}
                with self.assertRaises(manager.BridgeError):
                    manager.check_config({"views":[{"cards":[{"tap_action":bad}]}]})

    def test_hvac_other_targets_disallowed_and_nest_heat_preserved(self):
        nest={"action":"perform-action","perform_action":"climate.set_hvac_mode",
              "target":{"entity_id":manager.NEST},"data":{"hvac_mode":"heat"}}
        manager.check_config({"views":[{"cards":[{"tap_action":nest}]}]})
        unknown={**nest,"target":{"entity_id":"climate.some_other_device"}}
        with self.assertRaises(manager.BridgeError):
            manager.check_config({"views":[{"cards":[{"tap_action":unknown}]}]})



if __name__ == "__main__":
    unittest.main()


class QuietExitTests(unittest.TestCase):
    def test_fixed_scope_and_schedule(self):
        cfg = manager.quiet_exit_config()
        self.assertEqual(cfg["triggers"], [{"trigger": "state", "entity_id": "event.bedroom_master_bdrm_spot_voice_event", "to": None}])
        self.assertEqual(cfg["conditions"][0], {"condition": "time", "after": "05:31:00", "before": "06:30:00", "weekday": ["mon", "tue", "wed", "thu", "fri"]})
        self.assertEqual(cfg["conditions"][1], {"condition": "state", "entity_id": "input_boolean.horus_heru_sleeping", "state": "on"})
        self.assertEqual(cfg["actions"], [{"action": "media_player.volume_set", "target": {"entity_id": "media_player.bedroom_master_bdrm_spot"}, "data": {"volume_level": 0.1}}])

    def test_installer_respects_one_time_marker(self):
        with patch.object(manager.os.path, "exists", return_value=True), patch.object(manager, "urlopen") as request:
            manager.install_quiet_exit_once()
        request.assert_not_called()
