"""Build and test the installed ish wheel outside the source tree.

Run with the project's Python and uv available. Runtime dependencies are exported
from uv.lock; wheel construction uses the declared PEP 517 build environment.
All installations, settings and generated shell files live in a temporary folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = ("stream_bash", "stream_zsh", "stream_tcsh")
INTEGRATIONS = (
    "bash.sh",
    "tcsh-bindings.csh",
    "tcsh-decision.csh",
    "tcsh-hide.csh",
    "tcsh-popup.csh",
    "tcsh.csh",
    "zsh.env",
    "zsh.rc",
    "zsh.zsh",
)
RESOURCES = (
    "ish/lang/locales/ko.json",
    "ish/stream/settings.py",
    *(f"ish/stream/bundled/{name}/{name}.py" for name in PROVIDERS),
    *(f"ish/stream/integrations/{name}" for name in INTEGRATIONS),
)
PROBE = r"""
import asyncio
import hashlib
import importlib.metadata
import json
import os
import sys
from pathlib import Path

import ish
from ish.plugin.manager import PluginManager
from ish.stream.extensions import Extensions

expected = json.load(sys.stdin)
package = Path(ish.__file__).resolve().parent
prefix = Path(sys.prefix).resolve()
assert package.is_relative_to(prefix), ('ish imported outside installation', package)
assert importlib.metadata.version('ish') == expected['version']
assert ish.__version__ == expected['version']
for name, digest in expected['resources'].items():
    path = package.parent / name
    assert path.resolve().is_relative_to(package), ('resource escaped package', name)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name

library = Path.cwd() / 'absent-libraries'
extensions = Extensions()
try:
    extensions.load(expected['providers'], library_dir=library)
    assert isinstance(extensions.manager, PluginManager)
    assert not extensions.has_ui
    for name in expected['providers']:
        module = extensions.get(name)
        assert Path(module.__file__).resolve().is_relative_to(package), name
    for shell in ('bash', 'zsh', 'tcsh'):
        directory = Path.cwd() / shell
        directory.mkdir()
        build = extensions.select_shell((shell, '-i'))
        argv, env = build(
            (shell, '-i'), dict(os.environ), directory,
            'package-smoke', str(directory / 'popup-socket'),
        )
        assert argv[0] == shell and env
        assert any(path.is_file() for path in directory.rglob('*')), shell
finally:
    assert asyncio.run(extensions.close()) == []
assert not library.exists()
print(json.dumps({'installed_package': str(package), 'resources': len(expected['resources']),
                  'providers': expected['providers'], 'shell_builders': 'passed'}))
"""


def check(uv: str, work: Path) -> None:
    """Install a fresh wheel and exercise its entry point, data and plugin loader."""
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        environment.pop(name, None)
    runtime_environment = dict(environment, HOME=str(work / "home"))
    Path(runtime_environment["HOME"]).mkdir()

    def run(arguments, *, cwd=ROOT, installed=False, **options):
        return subprocess.run(
            arguments,
            cwd=cwd,
            env=runtime_environment if installed else environment,
            check=True,
            text=True,
            **options,
        )

    requirements = work / "requirements.txt"
    run(
        [
            uv,
            "export",
            "--locked",
            "--no-default-groups",
            "--no-emit-project",
            "--format",
            "requirements.txt",
            "--output-file",
            str(requirements),
        ],
        stdout=subprocess.DEVNULL,
    )
    wheels = work / "wheels"
    run([uv, "build", "--wheel", "--python", sys.executable, "--out-dir", str(wheels)])
    (wheel,) = wheels.glob("ish-*.whl")
    installation = work / "installation"
    run([uv, "venv", "--no-project", "--python", sys.executable, str(installation)])
    python = installation / "bin/python"
    run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--require-hashes",
            "--no-deps",
            "--only-binary",
            ":all:",
            "--link-mode",
            "copy",
            "-r",
            str(requirements),
        ]
    )
    run([uv, "pip", "install", "--python", str(python), "--no-deps", str(wheel)])

    outside = work / "outside"
    outside.mkdir()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected = {
        "version": project["project"]["version"],
        "providers": PROVIDERS,
        "resources": {
            name: hashlib.sha256((ROOT / "src" / name).read_bytes()).hexdigest()
            for name in RESOURCES
        },
    }
    run(
        [str(python), "-I", "-c", PROBE],
        cwd=outside,
        installed=True,
        input=json.dumps(expected),
    )

    settings = outside / "absent-settings"
    executable = installation / "bin/ish"
    for option, marker in (
        ("--help", "사용법:"),
        ("--version", "ish " + expected["version"]),
        ("--diagnose", "ish 환경 진단"),
    ):
        result = run(
            [str(executable), "--home", str(settings), "--lang", "ko", option],
            cwd=outside,
            installed=True,
            capture_output=True,
        )
        if marker not in result.stdout or result.stderr or settings.exists():
            raise RuntimeError(
                f"Installed {option} failed: {result.stdout!r} {result.stderr!r}"
            )
        if option == "--help" and "--demo" in result.stdout:
            raise RuntimeError("Installed help still exposes the removed demo option")
    # Queries must not execute executable Python configuration, even when it exists.
    settings.mkdir()
    marker = outside / "rc-executed"
    (settings / ".ishrc.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n"
        "raise RuntimeError('queries executed configuration')\n",
        encoding="utf-8",
    )
    for option in ("--help", "--version", "--diagnose"):
        result = run(
            [str(executable), "--home", str(settings), "--lang", "ko", option],
            cwd=outside,
            installed=True,
            capture_output=True,
        )
        if (
            marker.exists()
            or result.stderr
            or sorted(settings.iterdir()) != [settings / ".ishrc.py"]
        ):
            raise RuntimeError(f"Installed {option} executed or modified settings")
    for removed in ("ish-stream", "ish2", "ish-legacy"):
        if (installation / "bin" / removed).exists():
            raise RuntimeError(
                f"Installed package still exposes the old {removed} command"
            )
    for retained in ("ish-vt",):
        if not (installation / "bin" / retained).is_file():
            raise RuntimeError(f"Installed package lost the {retained} command")
    print(
        "Installed ish entry point, read-only queries, Korean help/diagnosis and resources passed."
    )


def main() -> int:
    """Run the same artifact check locally and in the pull-request workflow."""
    if not sys.platform.startswith("linux"):
        print("Stream package checks require Linux or WSL", file=sys.stderr)
        return 2
    uv = shutil.which("uv")
    if uv is None:
        print("Stream package checks require uv", file=sys.stderr)
        return 2
    try:
        with tempfile.TemporaryDirectory(prefix="ish-package-") as directory:
            check(uv, Path(directory))
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"Stream package check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
