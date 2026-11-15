"""Run opt-in terminal, image and clipboard checks in isolated cached hosts."""

import os
import subprocess
import sys
from pathlib import Path


def main():
    """Run GUI tests in the current Linux/WSL Python environment."""
    root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "ISH_TEST_GUI": "1",
        "PYTHONPATH": str(root / "src") + os.pathsep + str(root / "tests"),
    }
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "unittest",
            "test_terminal_gui",
            "test_terminal_legacy_gui",
            "test_terminal_clipboard_gui",
            "test_terminal_selection_gui",
            "test_terminal_basics_gui",
            "test_terminal_appearance_gui",
            "-v",
        ],
        cwd=root,
        env=environment,
    )


if __name__ == "__main__":
    raise SystemExit(main())
