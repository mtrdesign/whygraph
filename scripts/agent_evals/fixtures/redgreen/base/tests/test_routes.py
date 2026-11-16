import unittest

from mini.routes import handle


class RoutesTest(unittest.TestCase):
    def test_list_newest_first(self) -> None:
        ids = [note["id"] for note in handle("GET", "/notes")]
        self.assertEqual(ids, [2, 1])
