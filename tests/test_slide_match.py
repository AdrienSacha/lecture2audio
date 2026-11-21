"""Whether the slides belong to the lecture (`sources.slide_match`): computed in code before
any model call, reported at the top of report.md, and the slides of another lecture kept away
from the gap search unless --slides-all."""
import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from lecture2audio.cli import main
from lecture2audio.report import MISMATCH_TAG, slide_match_lines, slide_match_warning
from lecture2audio.sources import MATCH_DECK, slide_match

SAMPLE = pathlib.Path(__file__).resolve().parents[1] / "examples" / "sample_transcript.txt"

# a spoken lecture on bits, device registers, endianness and caches, as speech recognition
# writes it: fillers, no formulas, numbers in words
EN_TRANSCRIPT = [
    "So um today we start with bit manipulation, because you do it all the time. When you "
    "configure a device you read its register, you flip some bits, and you write the register "
    "back. To set a bit you use a bitwise or with a mask, to clear a bit you use a bitwise and "
    "with the complement of the mask, and to toggle a bit you use an exclusive or. The mask has "
    "a one exactly where you want to act. You build it with a shift: one shifted left by the "
    "position of the bit.",
    "Now the serial device, the uart. Its registers are memory mapped: each register of the "
    "device has an address, and reading that address does not read memory, it reads the "
    "device. The status register tells you whether the transmitter is ready, and the data "
    "register holds the byte you send. You poll the status register, you test the ready bit "
    "with a mask, and when it is set you write the byte into the data register. That is why "
    "the pointer must be volatile, so the compiler reads the register every time.",
    "Next, endianness. A word of four bytes is stored in four consecutive addresses. In little "
    "endian the least significant byte is at the lowest address, in big endian the most "
    "significant byte comes first. Intel machines and most risc five boards are little endian, "
    "network byte order is big endian. When you send a word over the network you must swap "
    "the bytes, otherwise the other machine reads a different number. You can test the byte "
    "order of your machine with a union or a pointer cast.",
    "Then caches. Memory is slow compared to the processor, so a small fast memory, the cache, "
    "keeps the recently used data. The cache is cut into lines of sixty four bytes. On a hit "
    "the data is in the cache, on a miss the line is fetched from memory. Temporal locality "
    "means you use the same data again soon, spatial locality means you use the neighbours. "
    "A direct mapped cache puts each line in exactly one place, a set associative cache "
    "chooses among a few ways, and the replacement policy picks the victim, often least "
    "recently used.",
    "Why does this matter for device registers? Because a register must never be cached. If "
    "the status register sat in the cache you would read an old value forever and the ready "
    "bit would never change. So the memory region of the device is marked uncached, and every "
    "access goes to the device itself. The same holds for writes: a write buffer could delay "
    "the byte, so you need a barrier before you poll the status again.",
    "Last point, performance. Walking an array row by row uses every byte of each cache line, "
    "walking it column by column misses on almost every access. The miss rate and the miss "
    "penalty give the average memory access time: hit time plus miss rate times miss penalty. "
    "So the order of your loops can change the speed by a factor of ten, with the same "
    "instructions, only because of the cache.",
]
EN_MATCHED = [
    "Bit Manipulation\nSet a bit: x | mask\nClear a bit: x & ~mask\nToggle a bit: x ^ mask\n"
    "mask = 1 << position",
    "Memory-Mapped Device Registers\nUART status register and data register\nPoll the ready "
    "bit, then write the byte\nvolatile pointer",
    "Endianness\nLittle endian: least significant byte at the lowest address\nBig endian: most "
    "significant byte first\nNetwork byte order is big endian: swap bytes",
    "Caches\nCache lines of 64 bytes\nHit, miss, temporal and spatial locality\nDirect mapped, "
    "set associative, least recently used replacement",
    "Devices and Caches\nDevice registers are uncached\nA stale status register never "
    "changes\nWrite buffer and barrier",
    "Performance\nAverage memory access time = hit time + miss rate x miss penalty\nRow by row "
    "versus column by column loops",
]
EN_OTHER = [  # the RV32I instruction set: another lecture of the same course
    "RV32I Instruction Formats\nSix formats, all 32 bits\nR, I, S, B, U, J\nopcode, funct3, "
    "funct7, rd, rs1, rs2",
    "Assembly Syntax\nadd t2, t0, t1\naddi t2, t0, -5\nlw t3, 8(t0)\nOne mnemonic per line, "
    "then operands",
    "Large Constants: lui\nImmediates have 12 bits\nlui rd, imm20 then addi\nSign extension of "
    "the immediate",
    "Pseudo-Instructions\nli, mv, nop, j, ret\nThe assembler replaces them by real "
    "instructions",
    "Encoding an R-Type\nfunct7 rs2 rs1 funct3 rd opcode\nopcode = 0110011\nadd t2, t0, t1 = "
    "0x005303B3",
    "Branches\nbeq, bne, blt, bge\nPC-relative offset in halfwords\nlabels resolved by the "
    "assembler",
]

FR_TRANSCRIPT = [
    "Alors aujourd'hui on parle de la mémoire cache. Le processeur est beaucoup plus rapide "
    "que la mémoire principale, donc on garde les données utilisées récemment dans une petite "
    "mémoire rapide, le cache. Le cache est découpé en lignes de soixante-quatre octets. Quand "
    "la donnée est dans le cache c'est un succès, sinon c'est un défaut de cache et on va "
    "chercher la ligne en mémoire.",
    "La localité temporelle, c'est réutiliser bientôt la même donnée. La localité spatiale, "
    "c'est utiliser les voisines. Un cache à correspondance directe place chaque ligne à un "
    "seul endroit, un cache associatif par ensembles choisit parmi plusieurs voies, et la "
    "politique de remplacement choisit la victime, souvent la moins récemment utilisée.",
    "Ensuite le boutisme. Un mot de quatre octets occupe quatre adresses consécutives. En petit "
    "boutiste l'octet de poids faible est à l'adresse la plus basse, en gros boutiste l'octet "
    "de poids fort vient en premier. L'ordre des octets du réseau est gros boutiste, donc il "
    "faut permuter les octets avant d'envoyer un mot.",
    "Enfin la performance. Parcourir un tableau ligne par ligne utilise chaque octet de la "
    "ligne de cache, le parcourir colonne par colonne provoque un défaut presque à chaque "
    "accès. Le temps moyen d'accès mémoire vaut le temps de succès plus le taux de défauts "
    "multiplié par la pénalité de défaut.",
]
FR_MATCHED = [
    "La mémoire cache\nLignes de cache de 64 octets\nSuccès et défaut de cache",
    "Localité\nLocalité temporelle et localité spatiale\nCorrespondance directe, associatif "
    "par ensembles\nRemplacement : la moins récemment utilisée",
    "Le boutisme\nPetit boutiste : octet de poids faible à l'adresse la plus basse\nGros "
    "boutiste : octet de poids fort en premier\nOrdre réseau : permuter les octets",
    "Performance\nTemps moyen d'accès = temps de succès + taux de défauts × pénalité\nParcours "
    "ligne par ligne ou colonne par colonne",
]
FR_OTHER = [
    "La photosynthèse\nChlorophylle et lumière\nDioxyde de carbone et eau donnent du glucose",
    "La phase claire\nLes thylakoïdes captent les photons\nProduction d'ATP et de NADPH",
    "Le cycle de Calvin\nFixation du carbone par la RuBisCO\nRégénération du ribulose",
    "Les plantes en C4\nAdaptation aux climats chauds\nMaïs et canne à sucre",
]


def lecture(paragraphs, times=3):
    """A transcript of some length: the paragraphs said again with small changes, as a
    teacher repeats; long enough for many windows of `sources.MATCH_WINDOW` words."""
    return "\n\n".join(p if k == 0 else f"{p} As I said, this is important."
                       for k in range(times) for p in paragraphs)


class Scores(unittest.TestCase):
    def test_a_matched_deck_matches(self):
        m = slide_match(EN_MATCHED, lecture(EN_TRANSCRIPT))
        self.assertFalse(m["low"])
        self.assertEqual(m["matched"], m["scored"], m["slides"])
        self.assertGreaterEqual(m["score"], MATCH_DECK)

    def test_a_deck_of_another_lecture_is_low(self):
        m = slide_match(EN_OTHER, lecture(EN_TRANSCRIPT))
        self.assertTrue(m["low"])
        self.assertEqual(m["matched"], 0, m["slides"])

    def test_each_slide_is_placed_where_it_is_said(self):
        text = lecture(EN_TRANSCRIPT, times=1)
        m = slide_match(EN_MATCHED, text)
        starts = [s["start"] for s in m["slides"]]
        self.assertEqual(starts, sorted(starts))  # the deck follows the talk
        words = text.split()
        first, last = m["slides"][2]["words"]
        self.assertIn("endian", " ".join(words[first:last]))
        self.assertEqual(m["slides"][0]["title"], "Bit Manipulation")

    def test_partial_overlap_keeps_the_deck_and_names_the_strays(self):
        deck = EN_MATCHED[:4] + EN_OTHER[:2]
        m = slide_match(deck, lecture(EN_TRANSCRIPT))
        self.assertFalse(m["low"])
        self.assertEqual([s["slide"] for s in m["slides"] if not s["matched"]], [5, 6])
        lines = "\n".join(slide_match_lines(m))
        self.assertIn("- Slide 5: RV32I Instruction Formats", lines)
        self.assertIn("The teacher may have skipped them", lines)
        self.assertEqual(slide_match_warning(m), "")

    def test_mostly_another_lecture_is_low_with_the_matching_slides_named(self):
        deck = EN_OTHER[:5] + [EN_MATCHED[2]]
        m = slide_match(deck, lecture(EN_TRANSCRIPT))
        self.assertTrue(m["low"])
        self.assertEqual([s["slide"] for s in m["slides"] if s["matched"]], [6])

    def test_french(self):
        text = lecture(FR_TRANSCRIPT)
        m = slide_match(FR_MATCHED, text)
        self.assertFalse(m["low"])
        self.assertEqual(m["matched"], m["scored"], m["slides"])
        other = slide_match(FR_OTHER, text)
        self.assertTrue(other["low"])
        self.assertEqual(other["matched"], 0)

    def test_blank_slides_and_empty_inputs_are_not_scored(self):
        m = slide_match(["", "12", EN_MATCHED[0]], lecture(EN_TRANSCRIPT))
        self.assertEqual([s["score"] is None for s in m["slides"]], [True, True, False])
        self.assertEqual(m["scored"], 1)
        self.assertFalse(slide_match([], "words")["low"])
        self.assertTrue(slide_match(EN_OTHER, "")["low"])


class Report(unittest.TestCase):
    def test_warning_names_the_flag(self):
        m = slide_match(EN_OTHER, lecture(EN_TRANSCRIPT))
        self.assertIn("--slides-all keeps every slide", slide_match_warning(m))
        m["kept_all"] = True
        self.assertIn("every slide is still used", slide_match_warning(m))
        self.assertIn("kept because of `--slides-all`", "\n".join(slide_match_lines(m)))
        self.assertEqual(slide_match_lines(None), [])


class EndToEnd(unittest.TestCase):
    def run_cli(self, root: pathlib.Path, *extra: str) -> tuple[str, pathlib.Path]:
        deck = root / "slides.md"
        deck.write_text("\n---\n".join(
            ["Link-State Routing\nEvery router knows the whole graph\nDijkstra least cost path"]
            + EN_OTHER), encoding="utf-8")
        out = root / "out"
        env = {k: v for k, v in os.environ.items() if k != "L2A_VAULT_DIR"}
        with mock.patch.dict(os.environ, env, clear=True):
            with contextlib.redirect_stdout(io.StringIO()) as log, \
                    self.assertRaises(SystemExit) as raised:
                main(["text", str(SAMPLE), "--slides", str(deck), "--llm", "dry",
                      "--language", "en", "--max-part-words", "60", "--out", str(out),
                      "--no-open-obsidian", "--no-pdf", *extra])
        self.assertEqual(raised.exception.code, 0)
        return log.getvalue(), out / "sample-transcript"

    def test_slides_of_another_lecture_are_warned_about_and_left_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            log, work = self.run_cli(pathlib.Path(tmp))
            self.assertIn(MISMATCH_TAG + " 1 of 7 slides", log)
            report = (work / "report.md").read_text(encoding="utf-8")
            top = report.split("\n## ")[0]
            self.assertIn("**Warning: the slides seem to belong to another lecture.**", top)
            self.assertIn("No part has open problems.", top)
            self.assertIn("- Slide 2: RV32I Instruction Formats", report)
            match = json.loads((work / "slide_match.json").read_text(encoding="utf-8"))
            self.assertEqual([s["matched"] for s in match["slides"]], [True] + [False] * 6)
            coverage = json.loads((work / "coverage.json").read_text(encoding="utf-8"))
            self.assertEqual([i["slide"] for i in coverage["slide_ideas"]], [1])  # gap search

    def test_slides_all_keeps_every_slide(self):
        with tempfile.TemporaryDirectory() as tmp:
            log, work = self.run_cli(pathlib.Path(tmp), "--slides-all")
            self.assertIn("every slide is still used (--slides-all)", log)
            report = (work / "report.md").read_text(encoding="utf-8")
            self.assertIn("kept because of `--slides-all`", report)
            coverage = json.loads((work / "coverage.json").read_text(encoding="utf-8"))
            self.assertEqual([i["slide"] for i in coverage["slide_ideas"]], list(range(1, 8)))


if __name__ == "__main__":
    unittest.main()
