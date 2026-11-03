import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from lecture2audio.spoken import (en_cardinal, en_ordinal, find_residue, fr_cardinal, fr_ordinal,
                                  normalize_spoken, number_to_words)


class EnglishNumbers(unittest.TestCase):
    def test_cardinals(self):
        cases = {0: "zero", 13: "thirteen", 21: "twenty-one", 100: "one hundred",
                 101: "one hundred one", 1250: "one thousand two hundred fifty",
                 1000000: "one million",
                 2147483647: "two billion one hundred forty-seven million four hundred "
                             "eighty-three thousand six hundred forty-seven"}
        for n, words in cases.items():
            self.assertEqual(en_cardinal(n), words)

    def test_ordinals(self):
        cases = {1: "first", 2: "second", 3: "third", 4: "fourth", 12: "twelfth",
                 20: "twentieth", 21: "twenty-first", 100: "one hundredth"}
        for n, words in cases.items():
            self.assertEqual(en_ordinal(n), words)

    def test_huge_numbers_are_read_digit_by_digit(self):
        self.assertEqual(number_to_words(10**15, "en"), " ".join(["one"] + ["zero"] * 15))


class FrenchNumbers(unittest.TestCase):
    def test_cardinals(self):
        cases = {0: "zéro", 16: "seize", 17: "dix-sept", 21: "vingt et un", 22: "vingt-deux",
                 70: "soixante-dix", 71: "soixante et onze", 77: "soixante-dix-sept",
                 80: "quatre-vingts", 81: "quatre-vingt-un", 91: "quatre-vingt-onze",
                 99: "quatre-vingt-dix-neuf", 100: "cent", 101: "cent un", 200: "deux cents",
                 201: "deux cent un", 280: "deux cent quatre-vingts", 1000: "mille",
                 1001: "mille un", 2000: "deux mille", 80000: "quatre-vingt mille",
                 200000: "deux cent mille", 1000000: "un million", 2000000: "deux millions",
                 1999: "mille neuf cent quatre-vingt-dix-neuf"}
        for n, words in cases.items():
            self.assertEqual(fr_cardinal(n), words, n)

    def test_ordinals(self):
        cases = {1: "premier", 2: "deuxième", 3: "troisième", 4: "quatrième", 5: "cinquième",
                 9: "neuvième", 20: "vingtième", 21: "vingt et unième", 80: "quatre-vingtième",
                 100: "centième"}
        for n, words in cases.items():
            self.assertEqual(fr_ordinal(n), words, n)
        self.assertEqual(fr_ordinal(1, feminine=True), "première")


class Normalisation(unittest.TestCase):
    def test_networking_example(self):
        out = normalize_spoken("The server at 192.168.0.1/24 uses port 8080; latency is 3.5 ms "
                               "(45% of 1,250 packets).")
        self.assertEqual(
            out, "The server at one nine two dot one six eight dot zero dot one slash "
                 "twenty-four uses port eight thousand eighty; latency is three point five "
                 "milliseconds (forty-five percent of one thousand two hundred fifty packets).")

    def test_code_words(self):
        out = normalize_spoken("Call my_buffer and getElementById, the 3rd one, value 0xFF, offset -5.")
        self.assertEqual(out, "Call my buffer and get element by id, the third one, value "
                              "hexadecimal F F, offset minus five.")

    def test_digits_glued_to_letters(self):
        self.assertEqual(normalize_spoken("Water is H2O."), "Water is H two O.")

    def test_french(self):
        out = normalize_spoken("Le 2e paquet fait 1 500 octets, soit 80% de 2,5 Mo.", "fr")
        self.assertEqual(out, "Le deuxième paquet fait mille cinq cents octets, soit "
                              "quatre-vingts pour cent de deux virgule cinq mégaoctets.")

    def test_symbols_are_spoken(self):
        out = normalize_spoken("if a != b && c <= d")
        self.assertEqual(out, "if a is not equal to b and c is less than or equal to d")

    def test_no_digit_or_symbol_survives(self):
        text = "Version 3.10.12 of file stdio.h: x = 2*y + 7 -> done | 15% #include"
        residue = find_residue(normalize_spoken(text))
        self.assertEqual(residue["digits"], 0)
        self.assertEqual(residue["symbols"], {})

    def test_residue_report(self):
        self.assertEqual(find_residue("a = 2 * 3"), {"digits": 2, "symbols": {"=": 1, "*": 1}})


if __name__ == "__main__":
    unittest.main()
