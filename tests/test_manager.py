import pathlib
import sys
import unittest

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

    def test_private_repo_and_paths(self):
        self.assertEqual(manager.PRIVATE_REPO, "hyhouse0001-lab/horus-home-os")
        self.assertEqual(manager.SNAPSHOT_PATH, "dashboard_sync/fuller-family-test.current.json")
        self.assertEqual(manager.PROPOSAL_PATH, "dashboard_sync/fuller-family-test.proposal.json")


if __name__ == "__main__":
    unittest.main()
