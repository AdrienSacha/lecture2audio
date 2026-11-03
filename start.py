"""Start the lecture2audio interface: python3 start.py

Opens http://127.0.0.1:8765/ in your browser. Ctrl+C stops it.
Options: --port 9000, --out FOLDER, --no-browser.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"

# The speech and voice models are installed in the project's .venv, not in the system
# Python: if another Python started this file, start it again with the .venv one.
if VENV_PYTHON.exists() and Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), __file__, *sys.argv[1:]])

sys.path.insert(0, str(ROOT / "src"))  # so that "import lecture2audio" finds src/lecture2audio

from lecture2audio.gui import main  # noqa: E402

if __name__ == "__main__":
    main()
