"""Hidden check for S3: ties round half-even."""

import unittest
from decimal import Decimal

from price import round_amount

CASES = [
    ("0.125", "0.12"),
    ("0.135", "0.14"),
    ("2.665", "2.66"),
    ("-0.125", "-0.12"),
    ("1.234", "1.23"),
]


class HiddenS3(unittest.TestCase):
    def test_table(self):
        for value, expected in CASES:
            with self.subTest(value=value):
                self.assertEqual(round_amount(value), Decimal(expected))


if __name__ == "__main__":
    unittest.main()
