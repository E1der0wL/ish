"""Run strict Linux/WSL terminal regression; real host and opt-in GUI checks are separate."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import shutil
import subprocess
import sys
import tomllib
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TERMINAL_PATTERN = "test_terminal_*.py"
REQUIRED_TERMINAL = (
    "test_terminal_basics",
    "test_terminal_checks",
    "test_terminal_cli",
    "test_terminal_clipboard",
    "test_terminal_clipboard_provider",
    "test_terminal_composition",
    "test_terminal_contracts",
    "test_terminal_cursor",
    "test_terminal_distribution",
    "test_terminal_engine",
    "test_terminal_extensions",
    "test_terminal_features",
    "test_terminal_geometry",
    "test_terminal_sixel",
    "test_terminal_optional",
    "test_terminal_graphics",
    "test_terminal_graphics_vt",
    "test_terminal_hookless",
    "test_terminal_host",
    "test_terminal_image_protocol",
    "test_terminal_inputs",
    "test_terminal_keys",
    "test_terminal_mouse",
    "test_terminal_mouse_policy",
    "test_terminal_native_features",
    "test_terminal_protocol",
    "test_terminal_selection",
    "test_terminal_session",
    "test_terminal_startup",
    "test_terminal_supplements",
    "test_terminal_terminfo",
    "test_terminal_vt",
)
SHARED_TESTS = (
    "test_plugin_loading",
    "test_plugin_dependencies",
    "test_i18n",
    "test_diagnostics",
    "test_logging",
    "test_management_store",
    "test_management_ui",
    "test_management_restore",
)
OPT_IN_GUI = (
    "test_terminal_appearance_gui",
    "test_terminal_basics_gui",
    "test_terminal_clipboard_gui",
    "test_terminal_gui",
    "test_terminal_legacy_gui",
    "test_terminal_selection_gui",
)
NATIVE_PROGRAMS = ("bash", "zsh", "tcsh", "vim")
TEST_PROGRAMS = ("infocmp", "tput", "Xvfb", "xclip")
NATIVE_PROBE = b"VT_READY"
LINT_PATHS = (
    "src/ish",
    "bundles",
    "tools/build_distribution.py",
    "tools/build_ghostty_vt.py",
    "tools/check_runtime.py",
    "tools/check_terminal.py",
    "tools/smoke_distribution.py",
    "tools/terminal_contracts.py",
    "tools/compare_terminal.py",
)
REQUIRED_HELPERS = (
    "tests/comparison_terminal.py",
    "tests/comparison_child.py",
    "tests/comparison_apps.py",
    "tests/comparison_images.py",
    "tests/terminal_presentation.py",
    "tests/terminal_keyboard.py",
)
HELPER_PATTERNS = (
    "terminal_*.py",
    "plugin_fixture.py",
    "startup_fixture.py",
    "management_*.py",
)


class StrictResult(unittest.TextTestResult):
    """A missing capability or unexpected skip cannot turn native coverage green."""

    def wasSuccessful(self):
        """Require every collected test to execute, in addition to ordinary success."""
        return super().wasSuccessful() and not self.skipped


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


def modules():
    """Require maintained boundaries while discovering additional non-GUI tests."""
    directory = ROOT / "tests"
    selected = {path.stem for path in directory.glob(TERMINAL_PATTERN)}
    selected.difference_update(OPT_IN_GUI)
    missing = set(REQUIRED_TERMINAL).difference(selected)
    missing.update(
        name for name in SHARED_TESTS if not (directory / f"{name}.py").is_file()
    )
    if missing:
        raise RuntimeError(
            "Required test modules are missing: " + ", ".join(sorted(missing))
        )
    return sorted(selected) + list(SHARED_TESTS)


def validate_cases(suite):
    """Require declared evidence in the actual suite, without collecting it twice."""
    if not (ROOT / "tools/terminal_contracts.py").is_file():
        # A different checkout on PYTHONPATH must not replace deleted local
        # evidence through Python's namespace-package fallback.
        raise RuntimeError(
            "Required evidence mapping is missing: tools.terminal_contracts"
        )
    from tools.terminal_contracts import REQUIRED_CASES, REQUIRED_CHECKS

    def identifiers(tests):
        for test in tests:
            if isinstance(test, unittest.TestSuite):
                yield from identifiers(test)
            else:
                yield test.id()

    observed = Counter(identifiers(suite))
    failures = []
    if not REQUIRED_CASES:
        raise RuntimeError("Required terminal case mapping is empty")
    for requirement, cases in (*REQUIRED_CASES.items(), ("CHECKS", REQUIRED_CHECKS)):
        if not cases:
            failures.append(f"{requirement}: no required cases")
        for case in cases:
            if observed[case] != 1:
                failures.append(f"{requirement}: {case} (collected {observed[case]})")
    if failures:
        raise RuntimeError(
            "Required terminal cases missing or duplicated: " + "; ".join(failures)
        )


def environment(*, lint):
    """Check native executables, locked dependencies, PTYs and the actual VT bridge."""
    if not sys.platform.startswith("linux"):
        raise RuntimeError("Terminal checks require Linux or WSL with controlling PTYs")
    if not hasattr(os, "WNOWAIT"):
        raise RuntimeError("Non-reaping waitid observation is required")
    executables = {name: shutil.which(name) for name in NATIVE_PROGRAMS}
    missing = [name for name, executable in executables.items() if executable is None]
    if missing:
        raise RuntimeError(
            "Required native programs are missing: " + ", ".join(missing)
        )
    missing = [name for name in TEST_PROGRAMS if shutil.which(name) is None]
    if missing:
        raise RuntimeError("Required test programs are missing: " + ", ".join(missing))
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
    import pty

    master, slave = pty.openpty()
    os.close(master)
    os.close(slave)
    from ish.terminal.vt import Terminal

    # Terminal owns the library path, ABI/version checks and C signatures. Reuse
    # that boundary rather than maintaining another native capability table here.
    try:
        with Terminal(12, 2, lambda _: None) as terminal:
            terminal.feed(NATIVE_PROBE)
            frame = terminal.frame()
            rendered = "".join(value for _, value in frame.lines[0]).rstrip()
            if rendered != NATIVE_PROBE.decode("ascii"):
                raise RuntimeError("Native VT feed/frame probe did not preserve text")
            print(f"libghostty-vt: {terminal.engine}", flush=True)
    except (AttributeError, ValueError) as error:
        raise RuntimeError(f"Native VT boundary is unavailable: {error}") from error
    for name, executable in executables.items():
        output = subprocess.check_output([executable, "--version"], text=True)
        if not output.splitlines():
            raise RuntimeError(f"Native version probe returned no output: {name}")
        print(f"{name}: {executable}\n{output.splitlines()[0]}", flush=True)


def lint_paths(selected):
    """Keep product, distribution and regression helper checks on an explicit scope."""
    paths = [ROOT / path for path in LINT_PATHS]
    paths.extend(ROOT / path for path in REQUIRED_HELPERS)
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
    """Run Ruff without modifying sources or creating its cache."""
    paths = lint_paths(selected)
    for arguments in (("check",), ("format", "--check")):
        subprocess.run(
            [sys.executable, "-m", "ruff", *arguments, "--no-cache", *paths],
            cwd=ROOT,
            check=True,
        )


def main(argv=None):
    """Run mandatory checks without treating GUI or real-host coverage as implied."""
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
        # Resolve any relative native library override from the project, matching
        # the test processes that run with the project as their working directory.
        os.chdir(ROOT)
        environment(lint=not options.tests_only)
        print(
            "Scope: native PTY regression. Optional GUI: tools/check_terminal_gui.py; "
            "actual Windows Terminal/WSL: tests/terminal_matrix.py (run separately).",
            flush=True,
        )
        if not options.tests_only:
            lint(selected)
        if not options.lint_only:
            return regression(selected, validate=validate_cases)
        return 0
    except (OSError, RuntimeError, ImportError, subprocess.SubprocessError) as error:
        print(f"Terminal checks failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
