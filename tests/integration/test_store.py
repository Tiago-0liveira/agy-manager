import tempfile
import unittest
from pathlib import Path

from agym.integration.store import Store


class StoreTests(unittest.TestCase):
    def test_instance_and_event_sequence(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            self.assertEqual(store.get_instance_id(), Store(Path(tmp)).get_instance_id())
            store.create_run({"run_id": "run-one", "last_seq": 0, "status": "running",
                              "created_at": "now"})
            for _ in range(3):
                store.append_event("run-one", {"type": "output", "payload": {}})
            self.assertEqual([e["seq"] for e in store.get_events("run-one", 1, 2)], [2, 3])
