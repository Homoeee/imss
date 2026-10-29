import unittest

from imss.core.models import Bundle
from imss.core.numbering import bundle_numbers


def bundle(key):
    return Bundle(key, '测试物品', '1.00', [])


class NumberingTests(unittest.TestCase):
    def test_legacy_subitems_become_consecutive_integers(self):
        labels = ['01', '24.1', '24.2', '34.1', '34.2', '35.1', '35.2', '36.1', '36.2', '40']
        groups = [bundle('manual:' + number) for number in labels]
        numbers, next_number = bundle_numbers(groups)
        self.assertEqual([numbers[group.key] for group in groups], [f'{n:02d}' for n in range(1, 11)])
        self.assertEqual(next_number, 11)

    def test_group_labels_do_not_control_output_numbers(self):
        groups = [bundle('invoice:a'), bundle('manual:01'), bundle('manual:03.1'), bundle('invoice:b')]
        numbers, next_number = bundle_numbers(groups)
        self.assertEqual([numbers[group.key] for group in groups], ['01', '02', '03', '04'])
        self.assertEqual(next_number, 5)

    def test_custom_start_and_three_digit_numbers(self):
        groups = [bundle('manual:1'), bundle('manual:01')]
        numbers, next_number = bundle_numbers(groups, start=99)
        self.assertEqual([numbers[group.key] for group in groups], ['99', '100'])
        self.assertEqual(next_number, 101)
        for start in (0, -1, 10000):
            with self.subTest(start=start), self.assertRaises(ValueError):
                bundle_numbers(groups, start=start)
