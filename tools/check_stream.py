"""Run the same stream lint and strict native regression checks locally and in CI."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import shutil
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STREAM_PATTERN = "test_stream_*.py"
REQUIRED_STREAM = (
    "test_stream_bash",
    "test_stream_checks",
    "test_stream_cli",
    "test_stream_distribution",
    "test_stream_extensions",
    "test_stream_extensions_shells",
    "test_stream_helper",
    "test_stream_host",
    "test_stream_logging",
    "test_stream_options",
    "test_stream_panel",
    "test_stream_posix",
    "test_stream_protocol",
    "test_stream_requests",
    "test_stream_scripts",
    "test_stream_scroll",
    "test_stream_shells",
    "test_stream_startup",
    "test_stream_state",
    "test_stream_zsh",
)
SHARED_TESTS = (
    "test_logging",
    "test_diagnostics",
    "test_i18n",
    "test_plugin_loading",
    "test_plugin_dependencies",
    "test_terminal_session",
    "test_tcsh_completion",
)
NATIVE_PROGRAMS = ("bash", "zsh", "tcsh", "vim")
LINT_PATHS = (
    "src/ish/stream",
    "src/ish/config.py",
    "src/ish/log.py",
    "src/ish/lang",
    "src/ish/parser/cli.py",
    "src/ish/plugin/dependencies.py",
    "src/ish/plugin/manager.py",
    "src/ish/runtime/diagnostics.py",
    "src/ish/runtime/fdio.py",
    "src/ish/runtime/startup.py",
    "src/ish/terminal/session.py",
    "examples/stream_plugins",
    "examples/ishrc.py",
    "tools/build_distribution.py",
    "tools/check_stream.py",
    "tools/check_stream_package.py",
)
HELPER_PATTERNS = ("stream_*.py", "plugin_fixture.py", "tcsh_completion.py")


class StrictResult(unittest.TextTestResult):
    """A missing capability or unexpected skip cannot turn native coverage green."""

    def wasSuccessful(self):
        """Require every collected test to execute, in addition to ordinary success."""
        return super().wasSuccessful() and not self.skipped


def modules():
    """Discover new stream tests while requiring the existing contract boundaries."""
    directory = ROOT / "tests"
    selected = {path.stem for path in directory.glob(STREAM_PATTERN)}
    missing = set(REQUIRED_STREAM).difference(selected)
    missing.update(
        name for name in SHARED_TESTS if not (directory / f"{name}.py").is_file()
    )
    if missing:
        raise RuntimeError(
            "Required test modules are missing: " + ", ".join(sorted(missing))
        )
    return sorted(selected) + list(SHARED_TESTS)


def environment(*, lint):
    """Fail early on missing Linux capabilities or undeclared dependency versions."""
    if not sys.platform.startswith("linux"):
        raise RuntimeError("Stream checks require Linux or WSL with controlling PTYs")
    if not hasattr(os, "WNOWAIT"):
        raise RuntimeError("Non-reaping waitid observation is required")
    from stream_fixture import SHELL_EXECUTABLE_ENV, shell_executable

    executables, missing = {}, []
    for name in NATIVE_PROGRAMS:
        variable = SHELL_EXECUTABLE_ENV.get(name)
        overridden = bool(variable and os.environ.get(variable))
        label = variable if overridden else name
        selected = shell_executable(name) if variable else name
        executable = shutil.which(selected)
        if executable is None:
            missing.append(f"{label}={selected}" if overridden else label)
        else:
            executables[label] = os.path.abspath(executable)
            if overridden:
                os.environ[variable] = executables[label]
    if missing:
        raise RuntimeError(
            "Required native programs are missing: " + ", ".join(missing)
        )
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = ".".join(map(str, sys.version_info[:3]))
    if version not in SpecifierSet(project["project"]["requires-python"]):
        raise RuntimeError(f"Python {version} does not satisfy pyproject.toml")
    requirements = list(project["project"]["dependencies"])
    if lint:
        requirements.extend(project["dependency-groups"]["dev"])
    for entry in requirements:
        requirement = Requirement(entry)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        installed = importlib.metadata.version(requirement.name)
        if installed not in requirement.specifier:
            raise RuntimeError(
                f"Installed {requirement.name} {installed} does not satisfy {requirement.specifier}"
            )
    # A runner can be Linux while its container cannot allocate a controlling PTY.
    import pty

    master, slave = pty.openpty()
    os.close(master)
    os.close(slave)
    for name, executable in executables.items():
        output = subprocess.check_output([executable, "--version"], text=True)
        print(f"{name}: {executable}\n{output.splitlines()[0]}", flush=True)


def lint_paths(selected):
    """Keep the checker and CI on one explicit source and test scope."""
    paths = [ROOT / path for path in LINT_PATHS]
    paths.extend(ROOT / "tests" / f"{name}.py" for name in selected)
    for pattern in HELPER_PATTERNS:
        paths.extend(sorted((ROOT / "tests").glob(pattern)))
    for path in paths:
        if not path.exists():
            raise RuntimeError(
                f"Required lint input is missing: {path.relative_to(ROOT)}"
            )
    return list(dict.fromkeys(str(path) for path in paths))


def lint(selected):
    """Check source without rewriting files or leaving a Ruff cache."""
    paths = lint_paths(selected)
    for arguments in (("check",), ("format", "--check")):
        subprocess.run(
            [sys.executable, "-m", "ruff", *arguments, "--no-cache", *paths],
            cwd=ROOT,
            check=True,
        )


def regression(selected, *, validate=None):
    """Run independent existing tests, rejecting empty modules and skipped cases."""
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    for name in selected:
        tests = loader.loadTestsFromName(name)
        if not tests.countTestCases():
            raise RuntimeError(f"Test module collected no tests: {name}")
        suite.addTests(tests)
    if validate is not None:
        try:
            validate(suite)
        except RuntimeError:
            # Preserve collection tracebacks when an evidence check rejects a
            # failed import before the runner can execute its _FailedTest.
            for error in loader.errors:
                print(error, file=sys.stderr)
            raise
    print(f"Running {suite.countTestCases()} tests; skips fail this check.", flush=True)
    result = unittest.TextTestRunner(verbosity=2, resultclass=StrictResult).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv=None):
    """Check prerequisites, lint and regression with a single reproducible command."""
    parser = argparse.ArgumentParser(description=__doc__)
    phase = parser.add_mutually_exclusive_group()
    phase.add_argument(
        "--lint-only", action="store_true", help="check formatting and lint only"
    )
    phase.add_argument(
        "--tests-only", action="store_true", help="run strict native regression only"
    )
    options = parser.parse_args(argv)
    paths = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
    sys.path[:0] = paths
    sys.dont_write_bytecode = True
    os.environ["PYTHONPATH"] = os.pathsep.join(paths)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        selected = modules()
        environment(lint=not options.tests_only)
        os.chdir(ROOT)
        if not options.tests_only:
            lint(selected)
        if not options.lint_only:
            return regression(selected)
        return 0
    except (OSError, RuntimeError, ImportError, subprocess.SubprocessError) as error:
        print(f"Stream checks failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
