"""Run real interactive programs and retain independent file/exit results."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from comparison_child import record


def main():
    """Run all required programs; absence or a nonzero result fails the comparison."""
    root, apps = map(Path, sys.argv[1:3])
    commands = {
        "vim": ["/usr/bin/vim", "-Nu", "NONE", "-n", "-i", "NONE"],
        "nvim": [
            str(apps / "nvim/nvim-linux-x86_64/bin/nvim"),
            "--clean",
            "-n",
            "-i",
            "NONE",
        ],
        "less": ["/usr/bin/less", "-R"],
        "htop": ["/usr/bin/htop"],
        "fzf": [
            str(apps / "fzf/fzf"),
            "--no-sort",
            "--no-mouse",
            "--height=100%",
            "--prompt=FZF_READY:",
        ],
        "python": [sys.executable, "-q"],
    }
    versions = {}
    for name, command in commands.items():
        versions[name] = subprocess.check_output(
            [command[0], "--version"], text=True
        ).splitlines()[0]
    record(root, "versions.json", versions)
    results = {}
    for name in ("vim", "nvim"):
        path = root / (name + ".txt")
        path.write_text("NATIVE_EDITOR\n")
        command = commands[name] + [
            "-c",
            "set noswapfile",
            "-c",
            f"call writefile(['ready'], '{root / (name + '-ready')}')",
            str(path),
        ]
        results[name] = subprocess.run(command, check=True).returncode
        record(
            root,
            name + "-result.json",
            {"exit": results[name], "text": path.read_text()},
        )
    content = root / "less.txt"
    content.write_text(
        "".join(f"LESS_{index:04d}\n" for index in range(220)) + "TARGET_LESS_RESULT\n"
    )
    results["less"] = subprocess.run(
        commands["less"] + [str(content)], check=True
    ).returncode
    record(root, "less-result.json", {"exit": results["less"]})
    results["htop"] = subprocess.run(commands["htop"], check=True).returncode
    record(root, "htop-result.json", {"exit": results["htop"]})
    source = root / "choices.txt"
    source.write_text("alpha\nneedle 한글\nomega\n")
    with source.open("r") as choices, (root / "fzf.txt").open("w") as output:
        results["fzf"] = subprocess.run(
            commands["fzf"], stdin=choices, stdout=output, check=True
        ).returncode
    record(
        root,
        "fzf-result.json",
        {"exit": results["fzf"], "text": (root / "fzf.txt").read_text()},
    )
    results["python"] = subprocess.run(commands["python"], check=True).returncode
    record(root, "apps-result.json", results)
    os.write(1, b"\r\nAPPS_DONE\r\n")
    sys.stdin.readline()


if __name__ == "__main__":
    main()
