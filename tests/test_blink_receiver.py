import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location("horus_blink_app", Path(__file__).resolve().parents[1] / "horus_blink_event_receiver" / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)

NOW = 1795830000
def stamp(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()
def event(**extra):
    return {"schema_version": 1, "source": "blink_live_events", "classification": "doorbell_press",
            "event_type": "button_press", "event_id": "a"*64, "received_at": stamp(NOW), **extra}

class ReceiverTests(unittest.TestCase):
    def test_press_calls_only_existing_script(self):
        request = Mock(side_effect=[{"entity_id": app.SCRIPT, "state":"off", "attributes":{}}, []])
        self.assertTrue(app.deliver(event(), NOW, request))
        self.assertEqual(request.call_args_list[1].args, ("/services/script/turn_on", {"entity_id": app.SCRIPT}))
    def test_motion_other_camera_stale_malformed_rejected(self):
        for e in (event(classification="camera_motion"), event(event_type="pir"), event(camera_name="other"),
                  event(received_at=stamp(NOW-31)), event(provider_timestamp=stamp(NOW-31)), event(event_id="bad")):
            request = Mock()
            self.assertFalse(app.deliver(e, NOW, request))
            request.assert_not_called()
    def test_duplicate_alexa_or_running_script(self):
        for state, last in (("off", NOW-3), ("on", NOW-20)):
            request = Mock(return_value={"entity_id":app.SCRIPT,"state":state,"attributes":{"last_triggered":stamp(last)}})
            self.assertFalse(app.deliver(event(), NOW, request))
            request.assert_called_once()
    def test_missing_script_retries(self):
        request = Mock(return_value={"entity_id":app.SCRIPT,"state":"unavailable"})
        with self.assertRaises(OSError):
            app.deliver(event(), NOW, request)
    def test_no_other_webhook(self):
        with self.assertRaises(ValueError):
            app.DirectDelivery("https://example.com/api/webhook/anything")
