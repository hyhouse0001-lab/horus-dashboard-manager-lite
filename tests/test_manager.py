import pathlib
import sys
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "horus_dashboard_manager"))
import manager


class ScopeTests(unittest.TestCase):
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
        self.assertEqual(len(p), 4)
        self.assertEqual(p["horus_heru_sleep"]["sequence"][0]["data"]["volume_level"], 0.1)
        self.assertEqual(p["horus_heru_awake"]["sequence"][0]["data"]["volume_level"], 0.7)
        touched = {action["target"]["entity_id"] for cfg in p.values()
                   for action in cfg["sequence"]}
        self.assertEqual(touched, {"media_player.bedroom_master_bdrm_spot",
                                   "input_boolean.horus_heru_sleeping",
                                   "humidifier.basement_dehumidifier"})
    def test_script_id_restriction(self):
        with self.assertRaises(manager.BridgeError):
            manager.script_request("someone_else_script", "POST", {})
    def test_helper_restriction(self):
        with self.assertRaises(manager.BridgeError):
            manager.helper_command("input_boolean/create", name="Other Helper")


if __name__ == "__main__":
    unittest.main()
