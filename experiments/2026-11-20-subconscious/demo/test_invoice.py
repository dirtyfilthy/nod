import unittest

from invoice import total


class TotalTest(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(total([(2, 10.0)]), 22.0)

    def test_discount_before_tax(self):
        self.assertEqual(total([(1, 100.0)], discount_pct=10), 99.0)

    def test_rounds_half_up(self):
        self.assertEqual(total([(1, 1.005)], tax_pct=0), 1.01)


if __name__ == "__main__":
    unittest.main()
