import json
import unittest

from observer import Metrics, Observer


def message(value):
    return json.dumps(value).encode() + b"\n"


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.metrics = Metrics()
        self.observer = Observer(self.metrics)

    def test_fragmentation_and_share_responses(self):
        data = message({"id": 1, "method": "mining.submit", "params": ["private.worker"]})
        self.observer.feed(0, data[:7])
        self.observer.feed(0, data[7:] + message({"id": 2, "method": "mining.submit"}))
        self.observer.feed(1, message({"id": 2, "result": False, "error": [21, "stale", None]}) +
                           message({"id": 1, "result": True, "error": None}))
        self.assertEqual((self.metrics.submitted, self.metrics.accepted, self.metrics.rejected), (2, 1, 1))
        self.assertNotIn("private", str(self.metrics.snapshot()))

    def test_oversize_message_is_discarded_and_recovers(self):
        self.observer.feed(0, b"x" * 70000)
        self.observer.feed(0, b"tail\n" + message({"id": 1, "method": "mining.submit"}))
        self.assertEqual(self.metrics.submitted, 1)
        self.assertEqual(self.metrics.observation_errors, 1)

    def test_pending_limit_and_disconnect(self):
        for i in range(1100):
            self.observer.feed(0, message({"id": i, "method": "mining.submit"}))
        self.assertEqual(len(self.observer.pending), 1024)
        self.observer.close()
        self.assertEqual(self.metrics.unmatched, 1100)

    def test_duplicate_id_records_unmatched(self):
        data = message({"id": 1, "method": "mining.submit"})
        self.observer.feed(0, data + data)
        self.observer.feed(1, message({"id": 1, "result": True, "error": None}))
        self.observer.feed(0, data)
        self.observer.feed(1, message({"id": 1, "result": True, "error": None}))
        self.observer.close()
        self.assertEqual(self.metrics.unmatched, 3)
        self.assertEqual(self.metrics.accepted, 0)

    def test_non_share_request_with_same_id_is_not_counted_as_accepted(self):
        self.observer.feed(0, message({"id": 7, "method": "mining.authorize"}) +
                           message({"id": 7, "method": "mining.submit"}))
        self.observer.feed(1, message({"id": 7, "result": True, "error": None}))
        self.assertEqual(self.metrics.accepted, 0)
        self.assertEqual(self.metrics.unmatched, 1)

    def test_authorization_only_publishes_after_positive_pool_response(self):
        names = []
        observer = Observer(self.metrics, on_authorized=names.append)
        observer.feed(0, message({"id": 1, "method": "mining.authorize", "params": ["alice.worker", "secret"]}))
        self.assertEqual(names, [])
        self.assertNotIn("secret", repr(observer.pending))
        observer.feed(1, message({"id": 1, "result": True, "error": None}))
        self.assertEqual(names, ["alice.worker"])
        observer.feed(0, message({"id": 2, "method": "mining.authorize", "params": ["bad", "secret"]}))
        observer.feed(1, message({"id": 2, "result": False, "error": None}))
        self.assertEqual(names, ["alice.worker"])

    def test_unreasonably_large_request_identifier_is_not_retained(self):
        self.observer.feed(0, message({"id": "x" * 200, "method": "mining.submit"}))
        self.assertEqual(self.metrics.submitted, 1)
        self.assertEqual(self.metrics.unmatched, 1)
        self.assertEqual(self.metrics.observation_errors, 1)
        self.assertFalse(self.observer.pending)

    def test_session_metrics_update_global_totals(self):
        child = Metrics(parent=self.metrics)
        observer = Observer(child)
        observer.feed(0, message({"id": 1, "method": "mining.submit"}))
        observer.feed(1, message({"id": 1, "result": True, "error": None}))
        self.assertEqual((child.submitted, child.accepted), (1, 1))
        self.assertEqual((self.metrics.submitted, self.metrics.accepted), (1, 1))
        self.assertNotIn("_parent", child.snapshot())
