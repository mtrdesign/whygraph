import unittest

from mini import pricing


class PricingTest(unittest.TestCase):
    def test_bounds(self) -> None:
        self.assertEqual(pricing.low_bound(100.0), 100.0)
        self.assertEqual(pricing.high_bound(100.0), 125.0)
