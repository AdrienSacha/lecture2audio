import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from lecture2audio.outline import (fallback_outline, moves, noise_by_part, parse_outline, ranges,
                                   structure)

UNITS = [f"unit number {k} says something." for k in range(1, 11)]  # 5 words each


def node(title, units, children=()):
    return {"title": title, "units": units, "children": list(children)}


def good():
    return {"title": "Lecture", "parts": [
        node("Part A", [[1, 1]], [node("Point A1", [[2, 3]]), node("Point A2", [[4, 5]])]),
        node("Part B", [[7, 10]])], "noise": [[6, 6]],
        "terms": [{"term": "window", "part": 1}]}


class Parse(unittest.TestCase):
    def test_valid_outline_gets_ids_levels_and_terms(self):
        outline, errors = parse_outline(good(), UNITS, 1000)
        self.assertEqual(errors, [])
        a = outline.parts[0]
        self.assertEqual([n.id for n in a.walk()], ["1", "1.1", "1.2"])
        self.assertEqual([n.level for n in a.walk()], [1, 2, 2])
        self.assertEqual(outline.noise_units(), [6])
        self.assertEqual(outline.terms, [{"term": "window", "part": 1}])

    def test_single_child_is_folded_into_its_parent(self):
        data = good()
        data["parts"][1] = node("Part B", [[7, 8]], [node("Only point", [[9, 10]])])
        outline, errors = parse_outline(data, UNITS, 1000)
        self.assertEqual(errors, [])
        self.assertEqual(outline.parts[1].children, [])
        self.assertEqual(outline.parts[1].own_units(), [7, 8, 9, 10])

    def test_missing_and_doubled_units_are_reported(self):
        data = good()
        data["noise"] = [[5, 5]]
        _, errors = parse_outline(data, UNITS, 1000)
        text = " ".join(errors)
        self.assertIn("units 6 are in no node", text)
        self.assertIn("units 5 appear more than once", text)

    def test_moving_material_across_parts_is_refused(self):
        data = good()
        data["parts"][0]["children"][1]["units"] = [[4, 4], [8, 8]]
        data["parts"][1]["units"] = [[5, 5], [7, 7], [9, 10]]
        _, errors = parse_outline(data, UNITS, 1000)
        self.assertTrue(any("must follow the lecture's order" in e for e in errors), errors)

    def test_fourth_level_is_refused(self):
        data = good()
        data["parts"][0]["children"][0] = node("A1", [], [
            node("x", [[2, 2]], [node("deep", [[3, 3]]), node("deeper", [])]), node("y", [])])
        _, errors = parse_outline(data, UNITS, 1000)
        self.assertTrue(any("third level" in e for e in errors), errors)

    def test_part_over_the_word_limit_is_refused(self):
        _, errors = parse_outline(good(), UNITS, 15)
        self.assertTrue(any("part 1 has 25 words" in e for e in errors), errors)

    def test_term_with_unknown_part_is_refused(self):
        data = good()
        data["terms"] = [{"term": "window", "part": 7}]
        _, errors = parse_outline(data, UNITS, 1000)
        self.assertTrue(any("term" in e for e in errors), errors)

    def test_not_an_outline(self):
        self.assertTrue(parse_outline({"parts": []}, UNITS, 1000)[1])


class Derived(unittest.TestCase):
    def test_moves_inside_a_part_are_found(self):
        data = good()
        data["parts"][0]["children"] = [node("A1", [[2, 2], [5, 5]]), node("A2", [[3, 4]])]
        outline, errors = parse_outline(data, UNITS, 1000)
        self.assertEqual(errors, [])
        found = moves(outline.parts[0])
        self.assertEqual([(m["node"], m["units"]) for m in found], [("1.2", [3, 4])])

    def test_noise_goes_to_the_part_that_contains_or_follows_it(self):
        data = good()
        data["parts"][0]["units"] = []
        data["parts"][0]["children"][0]["units"] = [[2, 3]]
        data["noise"] = [[1, 1], [6, 6]]
        outline, errors = parse_outline(data, UNITS, 1000)
        self.assertEqual(errors, [])
        self.assertEqual(noise_by_part(outline), {"1": [1], "2": [6]})

    def test_structure_lists_sections_and_previews(self):
        data = good()
        data["parts"][0]["children"][0] = node("A1", [], [node("a", [[2, 2]]), node("b", [[3, 3]])])
        outline, _ = parse_outline(data, UNITS, 1000)
        self.assertEqual(structure(outline.parts[0]), {
            "sections": ["1", "1.1", "1.1.1", "1.1.2", "1.2"], "preview_items": ["1.1", "1.2"],
            "section_previews": {"1.1": ["1.1.1", "1.1.2"]}})

    def test_fallback_covers_every_unit_once(self):
        outline = fallback_outline(UNITS, 16, "test")
        covered = [u for p in outline.parts for u in p.all_units()]
        self.assertEqual(covered, list(range(1, 11)))
        self.assertEqual(outline.fallback, "test")

    def test_ranges(self):
        self.assertEqual(ranges([9, 3, 4, 5, 11]), "3-5, 9, 11")


if __name__ == "__main__":
    unittest.main()
