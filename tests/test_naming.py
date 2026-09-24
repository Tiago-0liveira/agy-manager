from __future__ import annotations

import unittest

from agym.naming import (
    generate_account_names,
    get_letter_name,
    get_numeric_name,
)


class TestNaming(unittest.TestCase):
    def test_get_numeric_name(self) -> None:
        self.assertEqual(get_numeric_name(0), "1")
        self.assertEqual(get_numeric_name(9), "10")
        self.assertEqual(get_numeric_name(99), "100")
        with self.assertRaises(ValueError):
            get_numeric_name(-1)

    def test_get_letter_name_boundaries(self) -> None:
        # Index 0: 'A' (Account 1)
        self.assertEqual(get_letter_name(0), "A")
        # Index 25: 'Z' (Account 26)
        self.assertEqual(get_letter_name(25), "Z")
        # Index 26: 'Aa' (Account 27)
        self.assertEqual(get_letter_name(26), "Aa")
        # Index 27: 'Ab' (Account 28)
        self.assertEqual(get_letter_name(27), "Ab")
        # Index 51: 'Az' (Account 52)
        self.assertEqual(get_letter_name(51), "Az")
        # Index 52: 'Ba' (Account 53)
        self.assertEqual(get_letter_name(52), "Ba")
        # Index 701: 'Zz' (Account 702)
        self.assertEqual(get_letter_name(701), "Zz")
        # Index 702: 'Aaa' (Account 703)
        self.assertEqual(get_letter_name(702), "Aaa")
        # Negative index
        with self.assertRaises(ValueError):
            get_letter_name(-1)

    def test_generate_account_names_empty(self) -> None:
        self.assertEqual(generate_account_names(0, "num"), [])
        self.assertEqual(generate_account_names(0, "letter"), [])
        with self.assertRaises(ValueError):
            generate_account_names(-1, "num")

    def test_generate_account_names_num(self) -> None:
        # Count 1
        self.assertEqual(generate_account_names(1, "num"), ["1"])
        # Count 10
        expected_10 = [str(i) for i in range(1, 11)]
        self.assertEqual(generate_account_names(10, "num"), expected_10)
        # Count 100
        expected_100 = [str(i) for i in range(1, 101)]
        self.assertEqual(generate_account_names(100, "num"), expected_100)

    def test_generate_account_names_letter_boundaries(self) -> None:
        # Count 1
        self.assertEqual(generate_account_names(1, "letter"), ["A"])
        # Count 26
        names_26 = generate_account_names(26, "letter")
        self.assertEqual(len(names_26), 26)
        self.assertEqual(names_26[0], "A")
        self.assertEqual(names_26[25], "Z")

        # Count 27
        names_27 = generate_account_names(27, "letter")
        self.assertEqual(names_27[26], "Aa")

        # Count 52
        names_52 = generate_account_names(52, "letter")
        self.assertEqual(names_52[51], "Az")

        # Count 53
        names_53 = generate_account_names(53, "letter")
        self.assertEqual(names_53[52], "Ba")

        # Count 702
        names_702 = generate_account_names(702, "letter")
        self.assertEqual(names_702[701], "Zz")

        # Count 703
        names_703 = generate_account_names(703, "letter")
        self.assertEqual(names_703[702], "Aaa")

    def test_generate_account_names_case_insensitivity(self) -> None:
        self.assertEqual(generate_account_names(2, "NUM"), ["1", "2"])
        self.assertEqual(generate_account_names(2, "Num"), ["1", "2"])
        self.assertEqual(generate_account_names(2, "LETTER"), ["A", "B"])
        self.assertEqual(generate_account_names(2, "Letter"), ["A", "B"])

    def test_generate_account_names_invalid_mode(self) -> None:
        with self.assertRaises(ValueError):
            generate_account_names(5, "invalid")
        with self.assertRaises(ValueError):
            generate_account_names(5, "")


if __name__ == "__main__":
    unittest.main()
