"""Hidden check for S2: the default export order is by id."""

import io
import unittest

from export import write


class HiddenS2(unittest.TestCase):
    def test_default_order_is_by_id(self):
        rows = [
            {"id": 3, "name": "alice"},
            {"id": 1, "name": "carol"},
            {"id": 2, "name": "bob"},
        ]
        buf = io.StringIO()
        write(rows, buf)
        self.assertEqual(buf.getvalue().splitlines(), ["1,carol", "2,bob", "3,alice"])


if __name__ == "__main__":
    unittest.main()
