import unittest
from unittest.mock import patch

from z2m2zha import ha


class FakeWS:
    made = 0

    def __init__(self, url, timeout, verify_tls=True):
        FakeWS.made += 1
        self.s = self
        self.closed = False
        self.inbox = [{"type": "auth_required"}, {"type": "auth_ok"}]

    def close(self):
        self.closed = True

    def send(self, obj):
        if "id" in obj:
            self.inbox.append({"id": obj["id"], "type": "result", "success": True, "result": obj["type"]})

    def recv(self):
        return self.inbox.pop(0)


class Reconnect(unittest.TestCase):
    def setUp(self):
        FakeWS.made = 0
        self.enterContext(patch.object(ha, "_WS", FakeWS))
        self.enterContext(patch.object(ha, "endpoints", return_value=("http://x/api", "ws://x", "t", None)))
        self.clock = self.enterContext(patch.object(ha.time, "monotonic", return_value=0.0))

    def test_busy_connection_is_kept(self):
        c = ha.HA({"ha": {}})
        self.clock.return_value = 10.0
        self.assertEqual(c("ping"), "ping")
        self.assertEqual(FakeWS.made, 1)

    def test_idle_connection_is_replaced_before_the_command_is_sent(self):
        c = ha.HA({"ha": {}})
        old = c.ws
        self.clock.return_value = ha.HA.IDLE_RECONNECT + 1.0
        self.assertEqual(c("ping"), "ping")
        self.assertEqual(FakeWS.made, 2)
        self.assertTrue(old.closed)
        self.assertEqual(c("again"), "again")
        self.assertEqual(FakeWS.made, 2)


if __name__ == "__main__":
    unittest.main()
