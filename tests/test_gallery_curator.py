import unittest,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"horus_dashboard_manager"))
import gallery_curator as g
class CuratorTests(unittest.TestCase):
    def test_attribution(self):
        self.assertTrue(g.matches("Archibald J. Motley Jr.","Archibald Motley"))
        self.assertFalse(g.matches("After Archibald Motley","Archibald Motley"))
        self.assertTrue(g.matches("Lois Mailou Jones","Loïs Mailou Jones"))
    def test_rights_and_exact_artist(self):
        base={"id":1,"title":"Night","artist_title":"Horace Pippin","date_display":"1930","artwork_type_title":"Painting","image_id":"12345678-1234-1234-1234-123456789abc","is_public_domain":True}
        with patch.object(g,"query",return_value={"data":[base,dict(base,id=2,is_public_domain=False),dict(base,id=3,artist_title="Unknown")]}):
            rows=list(g.aic("Horace Pippin"))
            self.assertEqual([r["id"] for r in rows],["aic-1"])
    def test_network_scope(self):
        self.assertFalse(g.safe_url("https://www.artic.edu.evil.example/x",g.IMAGE_HOSTS))
        self.assertFalse(g.safe_url("http://www.artic.edu/x",g.IMAGE_HOSTS))
        self.assertFalse(g.safe_url("https://user@www.artic.edu/x",g.IMAGE_HOSTS))
        self.assertEqual(len(g.ARTISTS),40)
