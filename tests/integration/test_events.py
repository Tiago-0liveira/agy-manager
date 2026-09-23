import tempfile
import unittest
from pathlib import Path

from agym.integration import events
from agym.integration.store import Store


class EventTests(unittest.TestCase):
    def test_page_replays_after_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.create_run({"run_id": "run-one", "last_seq": 0, "status": "succeeded",
                              "created_at": "now"})
            for n in range(4):
                events.emit(store, "run-one", "output", {"text": str(n)})
            page = events.page(store, "run-one", 2, 1)
            self.assertEqual([e["seq"] for e in page["events"]], [3])
            self.assertEqual(page["next_cursor"], 3)
            self.assertTrue(page["has_more"])
