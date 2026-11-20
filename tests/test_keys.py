"""API keys in the key file (keys.py), always a temporary file, never the real one."""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lecture2audio import keys
from lecture2audio.llm import PRESETS


class KeysTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "keys.env"
        clean = {k: v for k, v in os.environ.items() if k not in keys.KNOWN}
        clean["L2A_KEYS_FILE"] = str(self.file)
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()
        keys._loaded.clear()

    def tearDown(self):
        self.env.stop()
        keys._loaded.clear()
        self.tmp.cleanup()

    def write(self, text, mode=0o600):
        self.file.write_text(text)
        self.file.chmod(mode)

    def test_every_provider_preset_is_known(self):
        for name, (_, env) in PRESETS.items():
            self.assertEqual(keys.KEY_ENV[name], env)

    def test_load_fills_unset_variables_and_the_shell_wins(self):
        self.write("# c\nDEEPSEEK_API_KEY=sk-file\nexport OPENAI_API_KEY='sk-stored'\nPATH=/evil\n")
        os.environ["OPENAI_API_KEY"] = "sk-shell"
        self.assertEqual(keys.load(), ["DEEPSEEK_API_KEY"])
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "sk-file")
        self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-shell")
        self.assertNotEqual(os.environ.get("PATH"), "/evil")  # only known key variables
        self.assertEqual(keys.source("DEEPSEEK_API_KEY"), "file")
        self.assertEqual(keys.source("OPENAI_API_KEY"), "shell")
        self.assertIsNone(keys.source("MOONSHOT_API_KEY"))

    def test_a_file_others_can_read_is_refused(self):
        self.write("DEEPSEEK_API_KEY=sk-file\n", 0o644)
        self.assertEqual(keys.load(), [])
        self.assertNotIn("DEEPSEEK_API_KEY", os.environ)
        with self.assertRaises(keys.KeyFileError):
            keys.save("deepseek", "sk-x")

    def test_save_is_private_and_keeps_other_keys(self):
        self.write("OPENAI_API_KEY=sk-o\n")
        self.assertEqual(keys.save("deepseek", "  sk-new \n"), "DEEPSEEK_API_KEY")
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)
        self.assertIn("OPENAI_API_KEY=sk-o", self.file.read_text())
        self.assertIn("DEEPSEEK_API_KEY=sk-new", self.file.read_text())
        self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "sk-new")
        self.assertTrue(keys.delete("deepseek"))
        self.assertNotIn("DEEPSEEK_API_KEY", os.environ)
        self.assertNotIn("DEEPSEEK", self.file.read_text())
        self.assertFalse(keys.delete("deepseek"))

    def test_new_file_is_created_private(self):
        keys.save("deepseek", "sk-new")
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)

    def test_bad_input_is_refused(self):
        for key in ("", "   ", "sk a", "sk'x"):
            with self.assertRaises(keys.KeyFileError):
                keys.save("deepseek", key)
        with self.assertRaises(keys.KeyFileError):
            keys.save("nobody", "sk-x")
        self.assertFalse(self.file.exists())

    def test_turned_off(self):
        self.write("DEEPSEEK_API_KEY=sk-file\n")
        os.environ["L2A_KEYS_FILE"] = "off"
        self.assertEqual(keys.load(), [])
        self.assertFalse(keys.available())
        with self.assertRaises(keys.KeyFileError):
            keys.save("deepseek", "sk-x")


if __name__ == "__main__":
    unittest.main()
