"""Hidden check for S1: no call path makes more than 3 attempts."""

import unittest

from client import fetch


class HiddenS1(unittest.TestCase):
    def test_attempts_are_capped(self):
        calls = []

        def send(url):
            calls.append(url)
            raise TimeoutError()

        with self.assertRaises(TimeoutError):
            fetch("http://x", send=send)
        self.assertLessEqual(len(calls), 3)
        self.assertGreaterEqual(len(calls), 1)

    def test_two_timeouts_then_success(self):
        calls = []

        def send(url):
            calls.append(url)
            if len(calls) <= 2:
                raise TimeoutError()
            return b"ok"

        self.assertEqual(fetch("http://x", send=send), b"ok")


if __name__ == "__main__":
    unittest.main()
