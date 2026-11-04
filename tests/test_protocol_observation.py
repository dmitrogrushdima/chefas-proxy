import json
import unittest

from observer import Metrics, Observer


def line(value):
    return json.dumps(value).encode() + b"\n"


class ProtocolObservationTests(unittest.TestCase):
    def setUp(self):
        self.observer = Observer(Metrics())

    def request(self, ident, method, params):
        self.observer.feed(0, line({"id": ident, "method": method, "params": params}))

    def reply(self, ident, result, error=None):
        self.observer.feed(1, line({"id": ident, "result": result, "error": error}))

    def test_observes_confirmed_handshake_and_updates(self):
        self.request(1, "mining.configure", [["version-rolling"], {}])
        self.request(2, "mining.subscribe", [])
        self.request(3, "mining.extranonce.subscribe", [])
        self.request(4, "mining.authorize", ["alice.worker", "password-secret"])
        self.reply(1, {"version-rolling": True, "version-rolling.mask": "1fffe000"})
        self.reply(2, [[['mining.notify', 'a']], "12345678", 4])
        self.reply(3, True)
        self.reply(4, True)
        self.observer.feed(1, line({"id": None, "method": "mining.set_difficulty", "params": [1024]}))
        state = self.observer.protocol
        self.assertTrue(state["configured"])
        self.assertTrue(state["version_rolling"])
        self.assertEqual(state["version_mask"], "1fffe000")
        self.assertEqual((state["extranonce1"], state["extranonce2_size"]), ("12345678", 4))
        self.assertTrue(state["subscribed"])
        self.assertTrue(state["extranonce_subscribed"])
        self.assertEqual(state["authorized_workers"], ["alice.worker"])
        self.assertEqual(state["observed_difficulty"], 1024)
        self.assertNotIn("password-secret", repr(state))
        self.observer.feed(1, line({"id": None, "method": "mining.set_extranonce", "params": ["abcdef00", 3]}))
        self.assertEqual((state["extranonce1"], state["extranonce2_size"]), ("abcdef00", 3))

    def test_rejected_or_ambiguous_response_does_not_claim_support(self):
        self.request(7, "mining.extranonce.subscribe", [])
        self.reply(7, False)
        self.assertFalse(self.observer.protocol["extranonce_subscribed"])
        self.request(8, "mining.subscribe", [])
        self.request(8, "mining.configure", [])
        self.reply(8, [[], "12345678", 4])
        self.assertFalse(self.observer.protocol["subscribed"])
        self.assertFalse(self.observer.protocol["configured"])

    def test_rejects_bad_difficulty_and_extranonce_observations(self):
        self.observer.feed(1, line({"method": "mining.set_difficulty", "params": [-1]}))
        self.observer.feed(1, line({"method": "mining.set_extranonce", "params": ["not hex", 4]}))
        self.assertIsNone(self.observer.protocol["observed_difficulty"])
        self.assertIsNone(self.observer.protocol["extranonce1"])
