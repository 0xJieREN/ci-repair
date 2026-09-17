import unittest

from src.ranges import inclusive_sum


class RangeTests(unittest.TestCase):
    def test_positive_interval(self):
        self.assertEqual(inclusive_sum(2, 5), 14)

    def test_single_value(self):
        self.assertEqual(inclusive_sum(3, 3), 3)

    def test_negative_interval(self):
        self.assertEqual(inclusive_sum(-3, -1), -6)

    def test_reversed_interval(self):
        with self.assertRaises(ValueError):
            inclusive_sum(5, 2)
