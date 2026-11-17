"""Verify the terminal check command refuses missing native support and coverage."""

from __future__ import annotations

import ctypes.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DEADLINE = 20
# Deliberately independent of the command's discovery list: deleting a maintained
# boundary from the checker must fail this test rather than reduce both lists.
CONTRACT_MODULES = (
    "test_terminal_basics",
    "test_terminal_checks",
    "test_terminal_cli",
    "test_terminal_clipboard",
    "test_terminal_clipboard_provider",
    "test_terminal_composition",
    "test_terminal_contracts",
    "test_terminal_cursor",
    "test_terminal_engine",
    "test_terminal_extensions",
    "test_terminal_features",
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
    "test_plugin_loading",
    "test_plugin_dependencies",
)
GUI_MODULES = (
    "test_terminal_appearance_gui",
    "test_terminal_basics_gui",
    "test_terminal_clipboard_gui",
    "test_terminal_gui",
    "test_terminal_legacy_gui",
    "test_terminal_selection_gui",
)
# Independent literal evidence, grouped only to avoid repeating class names.
# Do not import or derive these expectations from tools.terminal_contracts.
# IDs protect the stated examples; they are not claims of complete B/P coverage.
CASE_CONTRACTS = (
    (
        "FP01",
        "test_terminal_composition.FormattingPolicyTests",
        (
            "test_default_formats_reach_native_cells_without_host_identity",
            "test_disabled_formats_emit_neither_content_nor_optional_cleanup",
            "test_written_cursor_color_restores_even_when_execution_is_disabled",
        ),
    ),
    (
        "FP01",
        "test_terminal_features.FeaturePolicyTests",
        (
            "test_auto_is_rejected_without_an_implemented_support_query",
            "test_on_and_off_do_not_rewrite_observed_support",
            "test_auto_uses_anonymous_replies_and_never_a_version_name",
            "test_off_survives_geometry_loss_and_recovery",
            "test_forced_images_still_require_geometry_and_one_output_backend",
            "test_cursor_on_without_restorable_initial_color_does_not_write",
            "test_late_duplicate_and_cancelled_replies_cannot_upgrade_auto",
        ),
    ),
    (
        "IO02",
        "test_terminal_supplements.SustainedOutputTests",
        (
            "test_tcsh_output_survives_popup_and_repeated_resize",
            "test_bash_output_survives_popup_and_repeated_resize",
            "test_zsh_output_survives_popup_and_repeated_resize",
        ),
    ),
    (
        "HX08",
        "test_terminal_clipboard_provider.ClipboardProviderTests",
        (
            "test_default_write_policy_and_explicit_read_return_fresh_text",
            "test_reset_rejects_late_completion_and_old_timeout_during_new_read",
            "test_close_cancels_read_and_suppresses_late_reply",
            "test_consecutive_osc52_copy_and_read_preserve_order_without_yielding",
            "test_idle_waits_for_all_queued_copies_before_child_exit",
            "test_response_sink_failure_reaches_application_failure_boundary",
        ),
    ),
    (
        "HX08",
        "test_terminal_clipboard_provider.X11ProviderTests",
        (
            "test_independent_unicode_empty_and_incremental_roundtrip",
            "test_actual_vt_and_controlling_pty_return_provider_data_to_foreground_file",
        ),
    ),
    (
        "EXT01",
        "test_terminal_supplements.SharedDirectoryTests",
        ("test_one_directory_loads_output_keys_and_clipboard_without_ui",),
    ),
    (
        "EXT01",
        "test_terminal_keys.KeyMappingTests",
        (
            "test_reserved_keys_responses_and_protocols_cannot_be_mapped",
            "test_registration_limits_and_first_input_seal",
            "test_incomplete_and_oversized_csi_preserve_control_keys_and_following_text",
        ),
    ),
    (
        "EXT01",
        "test_terminal_keys.InputProcessorResponseTests",
        (
            "test_batched_cursor_report_preserves_adjacent_prefix_keys",
            "test_individual_cursor_report_preserves_pending_prefix",
            "test_reinserted_batch_filters_only_report_and_preserves_order",
        ),
    ),
    (
        "EXT01",
        "test_terminal_keys.KeyMappingPTYTests",
        (
            "test_sample_plugin_keys_follow_popup_focus_and_actual_pty_consumer",
            "test_unknown_responses_never_leak_to_stdin_and_paste_keeps_native_policy",
        ),
    ),
    (
        "EXT01",
        "test_terminal_terminfo.RemainingTerminfoContracts",
        ("test_advertised_inventory_has_no_unreviewed_entries",),
    ),
    (
        "IO01 IO05 IO08",
        "test_terminal_hookless.HooklessTests",
        (
            "test_bash_plain",
            "test_bash_plugins",
            "test_zsh_plain",
            "test_zsh_plugins",
            "test_tcsh_plain",
            "test_tcsh_plugins",
        ),
    ),
    (
        "IO02",
        "test_terminal_session.TerminalSessionTests",
        ("test_host_writer_backpressure_preserves_final_output",),
    ),
    (
        "IO03",
        "test_terminal_session.TerminalSessionTests",
        ("test_paused_exit_with_open_slave_drains_without_waiting_for_eof",),
    ),
    (
        "IO04 UI03",
        "test_terminal_cursor.CursorPTYTests",
        ("test_child_first_size_already_excludes_selected_bar",),
    ),
    (
        "IO04",
        "test_terminal_vt.TerminalVTTests",
        ("test_resize_updates_the_grid_and_native_size_report",),
    ),
    (
        "IO06",
        "test_terminal_cursor.RestorationPTYTests",
        (
            "test_initial_queries_precede_any_mode_change_and_exit_restores",
            "test_term_and_hup_restore_the_same_observed_values",
            "test_suspend_restores_and_resume_retains_the_same_native_shell",
        ),
    ),
    (
        "IO06 UI01",
        "test_terminal_composition.TerminalCompositionTests",
        ("test_failed_final_layout_still_restores_owned_host_metadata",),
    ),
    (
        "IO07",
        "test_terminal_contracts.BaselineContracts",
        ("test_processing_failure_cannot_be_reported_as_a_successful_feed",),
    ),
    (
        "VT01 HX02",
        "test_terminal_contracts.BaselineContracts",
        ("test_fragmentation_cancellation_and_query_identity",),
    ),
    (
        "VT02",
        "test_terminal_vt.TerminalVTTests",
        ("test_split_unicode_and_wide_cells_keep_text_and_cursor",),
    ),
    (
        "VT03",
        "test_terminal_contracts.BaselineContracts",
        ("test_forward_backward_and_cleared_tabs_use_physical_columns",),
    ),
    (
        "VT03",
        "test_terminal_contracts.TerminfoContracts",
        ("test_cursor_capabilities_reach_the_expected_cell",),
    ),
    (
        "VT04",
        "test_terminal_contracts.BaselineContracts",
        (
            "test_horizontal_margins_limit_wrapping_and_scrolling",
            "test_wrap_disable_and_carriage_return_cancel_pending_wrap",
        ),
    ),
    (
        "VT05",
        "test_terminal_contracts.BaselineContracts",
        ("test_insert_delete_repeat_and_erase_are_positioned",),
    ),
    (
        "VT05",
        "test_terminal_contracts.TerminfoContracts",
        ("test_edit_capabilities_produce_expected_text",),
    ),
    (
        "VT06",
        "test_terminal_contracts.BaselineContracts",
        (
            "test_region_scroll_reverse_index_and_next_line_keep_outer_rows",
            "test_history_erase_does_not_erase_the_visible_screen",
        ),
    ),
    (
        "VT07",
        "test_terminal_contracts.BaselineContracts",
        (
            "test_alternate_47_1047_and_saved_cursor_1048_have_distinct_effects",
            "test_1049_resize_and_repeated_exit_keep_primary_text",
        ),
    ),
    (
        "VT08",
        "test_terminal_vt.TerminalVTTests",
        ("test_default_colors_and_sgr_resets_keep_host_defaults",),
    ),
    (
        "VT08",
        "test_terminal_startup.StartupTests",
        ("test_color_depth_preserves_explicit_rgb_and_respects_ptk_environment",),
    ),
    ("VT09", "test_terminal_vt.TerminalVTTests", ("test_cursor_erase_and_rgb_style",)),
    (
        "VT11",
        "test_terminal_cursor.NativeCursorTests",
        ("test_native_defaults_wait_for_explicit_reset",),
    ),
    (
        "VT11",
        "test_terminal_composition.TerminalCompositionTests",
        ("test_repeated_reset_does_not_overwrite_restored_cursor_modes",),
    ),
    (
        "VT12",
        "test_terminal_contracts.BaselineContracts",
        ("test_soft_reset_resets_input_modes_without_erasing_text",),
    ),
    (
        "VT13",
        "test_terminal_contracts.BaselineContracts",
        ("test_dec_line_drawing_and_selective_reset",),
    ),
    (
        "VT14",
        "test_terminal_native_features.NativeFeaturesTests",
        ("test_hold_captures_exact_color_boundaries_inside_one_write",),
    ),
    (
        "VT15",
        "test_terminal_vt.TerminalVTTests",
        ("test_selection_never_uses_anchors_from_another_screen_or_reset",),
    ),
    (
        "IN01",
        "test_terminal_host.HostProbeTests",
        ("test_mouse_arrow_cpr_alt_and_lone_escape_keep_ptk_semantics",),
    ),
    (
        "IN01",
        "test_terminal_vt.TerminalVTTests",
        ("test_cursor_keys_paste_and_controls_follow_terminal_modes",),
    ),
    (
        "IN02 HX02",
        "test_terminal_contracts.TerminfoContracts",
        ("test_all_advertised_function_keys_match_native_modifier_encoding",),
    ),
    (
        "IN02",
        "test_terminal_contracts.TerminfoContracts",
        ("test_advertised_navigation_matches_application_cursor_mode",),
    ),
    (
        "IN02 HX01",
        "test_terminal_contracts.LiveQueryContracts",
        ("test_three_shells_return_queries_and_keys_to_the_native_reader",),
    ),
    (
        "IN04",
        "test_terminal_host.HostInputTests",
        ("test_real_reader_preserves_paste_as_one_event",),
    ),
    (
        "IN06",
        "test_terminal_composition.TerminalCompositionTests",
        ("test_closing_popup_while_host_unfocused_never_claims_native_focus",),
    ),
    (
        "IN07",
        "test_terminal_mouse_policy.MousePolicyTests",
        ("test_auto_mouse_reaches_actual_pty_consumer_and_file_once",),
    ),
    (
        "IN07",
        "test_terminal_cli.TerminalCLITests",
        ("test_vim_mouse_changes_cursor_and_keeps_primary_history_separate",),
    ),
    (
        "IN09",
        "test_terminal_mouse_policy.MousePolicyTests",
        ("test_default_and_keyboard_surfaces_leave_host_selection_available",),
    ),
    (
        "IN09",
        "test_terminal_selection.SelectionTests",
        ("test_single_double_and_triple_click_use_native_word_and_line_ranges",),
    ),
    (
        "IN10",
        "test_terminal_vt.TerminalVTTests",
        ("test_alternate_screen_does_not_borrow_primary_history",),
    ),
    (
        "IN12",
        "test_terminal_contracts.SearchContracts",
        ("test_history_search_selects_and_reveals_matches",),
    ),
    (
        "IN12",
        "test_terminal_composition.TerminalCompositionTests",
        ("test_selected_search_popup_routes_real_keys_to_native_history",),
    ),
    (
        "IN13",
        "test_terminal_composition.TerminalCompositionTests",
        ("test_popup_input_focus_results_and_stale_completion",),
    ),
    (
        "HX02",
        "test_terminal_contracts.TerminfoContracts",
        ("test_saved_cursor_tabs_wrap_and_screen_clear_use_database_bytes",),
    ),
    (
        "HX03",
        "test_terminal_contracts.BaselineContracts",
        ("test_queries_report_only_virtual_modes_cursor_and_sizes",),
    ),
    (
        "HX04",
        "test_terminal_native_features.NativeFeaturesTests",
        ("test_palette_updates_preserve_child_override_until_osc_reset",),
    ),
    (
        "HX08",
        "test_terminal_clipboard.ClipboardTests",
        (
            "test_unverified_host_and_disabled_policy_never_claim_success",
            "test_timeout_and_wrong_identity_cannot_complete_a_later_request",
        ),
    ),
    (
        "HX12",
        "test_terminal_startup.StartupTests",
        ("test_diagnose_explains_unsupported_controls_without_claiming_host_support",),
    ),
    (
        "HX13",
        "test_terminal_contracts.BaselineContracts",
        ("test_native_diagnostics_do_not_dereference_or_capture_payloads",),
    ),
    (
        "UI02",
        "test_terminal_composition.TerminalCompositionTests",
        ("test_default_is_full_native_grid_without_surfaces_or_reserved_keys",),
    ),
    (
        "UI03",
        "test_terminal_composition.TerminalCompositionTests",
        ("test_bar_always_leaves_a_native_row_when_outer_terminal_shrinks",),
    ),
    (
        "UI04",
        "test_terminal_composition.TerminalCompositionTests",
        ("test_popup_covering_either_half_of_wide_grapheme_restores_it",),
    ),
    (
        "UI05",
        "test_terminal_composition.TerminalCompositionTests",
        ("test_async_plugin_failure_diagnoses_and_restores_focus",),
    ),
    (
        "UI06",
        "test_terminal_startup.StartupTests",
        (
            "test_queries_do_not_execute_rc_plugins_shell_or_native_library",
            "test_language_is_cli_then_lang_then_builtin_english",
            "test_saved_vt_plugins_persist_without_loading_stream_plugins",
            "test_selected_file_logs_exclude_native_payload_and_obey_level",
        ),
    ),
    (
        "UI07",
        "test_terminal_startup.StartupTests",
        ("test_help_and_diagnostics_distinguish_vt_from_stream",),
    ),
    (
        "UI07",
        "test_terminal_vt.TerminalVTTests",
        ("test_zero_byte_limit_erases_and_disables_native_history",),
    ),
    (
        "UI08",
        "test_terminal_cursor.RestorationPTYTests",
        ("test_origin_restoration_keeps_the_primary_cursor_and_scroll_region",),
    ),
    (
        "IM08",
        "test_terminal_startup.StartupTests",
        ("test_image_rejections_are_counted_without_retaining_payload_in_logs",),
    ),
    (
        "HX06",
        "test_terminal_composition.TerminalCompositionTests",
        ("test_titles_are_published_without_host_identification_and_retained",),
    ),
    (
        "IN02 HX02",
        "test_terminal_contracts.TerminfoContracts",
        (
            "test_all_advertised_modified_navigation_matches_native_encoding",
            "test_all_advertised_keypad_entries_match_application_keypad_mode",
            "test_keypad_separator_obeys_native_modes_modifiers_and_event_actions",
        ),
    ),
)
EXPECTED_CASES = {}
for _requirements, _class_name, _methods in CASE_CONTRACTS:
    for _requirement in _requirements.split():
        EXPECTED_CASES.setdefault(_requirement, set()).update(
            f"{_class_name}.{method}" for method in _methods
        )
CHECK_CASES = {
    "test_terminal_checks.CheckCommandTests.test_required_evidence_matches_independent_ids_and_case_names",
    "test_terminal_checks.CheckCommandTests.test_each_mapped_case_is_required_exactly_once_in_the_collected_suite",
    "test_terminal_checks.CheckCommandTests.test_removed_or_renamed_case_fails_with_its_baseline_id",
    "test_terminal_checks.CheckCommandTests.test_load_tests_cannot_hide_an_existing_required_method",
    "test_terminal_checks.CheckCommandTests.test_missing_evidence_mapping_fails_before_test_execution",
}
ALL_CASES = CHECK_CASES.union(*EXPECTED_CASES.values())
PASSING_TEST = """import unittest
from pathlib import Path

class Contract(unittest.TestCase):
    def test_observed_result(self):
        Path(__file__).with_suffix(".ran").write_text("executed")
"""


def fixture(module):
    """Declare observable fake cases using the independent public test identities."""
    classes = {}
    for case in sorted(ALL_CASES):
        name, owner, method = case.split(".")
        if name == module:
            classes.setdefault(owner, []).append(method)
    body = PASSING_TEST
    for owner, methods in classes.items():
        body += f"\nclass {owner}(unittest.TestCase):\n"
        for method in methods:
            body += (
                f"    def {method}(self):\n"
                '        Path(__file__).with_suffix(".ran").write_text("executed")\n'
            )
    return body


@unittest.skipUnless(
    sys.platform.startswith("linux"), "requires the Linux check runner"
)
class CheckCommandTests(unittest.TestCase):
    """Run the public checker against isolated test files and the real VT boundary."""

    def setUp(self):
        """Keep native validation real while replacing suites with observable fixtures."""
        temporary = tempfile.TemporaryDirectory(prefix="ish-terminal-check-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "tools").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "src").symlink_to(ROOT / "src", target_is_directory=True)
        for name in ("check_terminal.py", "check_stream.py", "terminal_contracts.py"):
            shutil.copy2(ROOT / "tools" / name, self.root / "tools" / name)
        (self.root / "pyproject.toml").write_text(
            '[project]\nrequires-python = ">=3.12.14,<3.13"\ndependencies = []\n'
            "[dependency-groups]\ndev = []\n"
        )
        for name in CONTRACT_MODULES:
            (self.root / "tests" / f"{name}.py").write_text(fixture(name))
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        selected = self.env.get("ISH_GHOSTTY_LIB")
        if selected and Path(selected).is_file():
            self.env["ISH_GHOSTTY_LIB"] = str(Path(selected).resolve())

    def run_check(self, *, cwd=None):
        """Use a new interpreter so imports and native loading cannot be cached."""
        return subprocess.run(
            [
                sys.executable,
                str(self.root / "tools/check_terminal.py"),
                "--tests-only",
            ],
            cwd=self.root if cwd is None else cwd,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=DEADLINE,
            check=False,
        )

    def assert_no_tests_ran(self):
        """Observe that environment and collection failures happen before execution."""
        self.assertEqual(list((self.root / "tests").glob("*.ran")), [])

    def test_required_evidence_matches_independent_ids_and_case_names(self):
        """Removing a mapping cannot silently lower the protected coverage floor."""
        from tools.terminal_contracts import REQUIRED_CASES, REQUIRED_CHECKS

        self.assertEqual(
            {name: set(cases) for name, cases in REQUIRED_CASES.items()},
            EXPECTED_CASES,
        )
        self.assertEqual(set(REQUIRED_CHECKS), CHECK_CASES)
        self.assertEqual(len(REQUIRED_CHECKS), len(CHECK_CASES))
        for requirement, cases in REQUIRED_CASES.items():
            with self.subTest(requirement=requirement):
                self.assertEqual(len(cases), len(set(cases)))

    def test_each_mapped_case_is_required_exactly_once_in_the_collected_suite(self):
        """Keep module membership separate from individual test identity coverage."""
        from tools.check_terminal import validate_cases

        def suite(names):
            tests = []
            for name in names:
                test = unittest.FunctionTestCase(lambda: None)
                test.id = lambda name=name: name
                tests.append(unittest.TestSuite([test]))
            return unittest.TestSuite(tests)

        validate_cases(suite(ALL_CASES))
        for name in ALL_CASES:
            with self.subTest(case=name):
                with self.assertRaisesRegex(RuntimeError, "collected 0"):
                    validate_cases(suite(ALL_CASES - {name}))
                with self.assertRaisesRegex(RuntimeError, "collected 2"):
                    validate_cases(suite([*ALL_CASES, name]))
        with patch("tools.terminal_contracts.REQUIRED_CASES", {}):
            with self.assertRaisesRegex(RuntimeError, "mapping is empty"):
                validate_cases(suite(ALL_CASES))

    def test_removed_or_renamed_case_fails_with_its_baseline_id(self):
        """A nonempty existing module cannot conceal deletion of required behavior."""
        cases = (
            (
                "VT04",
                "test_terminal_contracts.BaselineContracts.test_horizontal_margins_limit_wrapping_and_scrolling",
            ),
            (
                "CHECKS",
                "test_terminal_checks.CheckCommandTests.test_required_evidence_matches_independent_ids_and_case_names",
            ),
        )
        for requirement, case in cases:
            module, _owner, method = case.split(".")
            path = self.root / "tests" / f"{module}.py"
            original = path.read_text()
            for replacement in ("removed_case", "test_renamed_case"):
                with self.subTest(case=case, replacement=replacement):
                    path.write_text(original.replace(method, replacement))
                    try:
                        result = self.run_check()
                    finally:
                        path.write_text(original)
                    self.assertEqual(
                        result.returncode, 2, result.stdout + result.stderr
                    )
                    self.assertIn(f"{requirement}: {case}", result.stderr)
                    self.assertIn("collected 0", result.stderr)
                    self.assert_no_tests_ran()

    def test_load_tests_cannot_hide_an_existing_required_method(self):
        """Validate the suite that will run, not names that happen to exist in source."""
        path = self.root / "tests/test_terminal_contracts.py"
        path.write_text(
            path.read_text()
            + "\ndef load_tests(loader, tests, pattern):\n"
            + "    return unittest.TestSuite(test for group in tests for test in group\n"
            + '        if not test.id().endswith("test_history_erase_does_not_erase_the_visible_screen"))\n'
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(
            "VT06: test_terminal_contracts.BaselineContracts.test_history_erase",
            result.stderr,
        )
        self.assert_no_tests_ran()

    def test_missing_evidence_mapping_fails_before_test_execution(self):
        """The evidence declaration itself is required, even when modules remain."""
        self.env["PYTHONPATH"] = os.pathsep.join(
            (str(ROOT), self.env.get("PYTHONPATH", ""))
        )
        (self.root / "tools/terminal_contracts.py").unlink()
        result = self.run_check()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("tools.terminal_contracts", result.stderr)
        self.assert_no_tests_ran()

    def test_new_tests_are_discovered_from_an_unrelated_directory(self):
        """The public command resolves its own project and runs added modules."""
        (self.root / "tests/test_terminal_added.py").write_text(PASSING_TEST)
        result = self.run_check(cwd=self.root.parent)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        expected = len(ALL_CASES) + len(CONTRACT_MODULES) + 1
        self.assertIn(f"Ran {expected} tests", result.stderr)
        self.assertEqual(
            len(list((self.root / "tests").glob("*.ran"))), len(CONTRACT_MODULES) + 1
        )
        self.assertIn("libghostty-vt:", result.stdout)
        self.assertIn("actual Windows Terminal/WSL:", result.stdout)
        self.assertIn("(run separately)", result.stdout)

    def test_every_required_module_is_protected(self):
        """A removed maintained boundary fails before native or test execution."""
        for name in CONTRACT_MODULES:
            with self.subTest(module=name):
                path = self.root / "tests" / f"{name}.py"
                path.unlink()
                try:
                    result = self.run_check()
                finally:
                    path.write_text(fixture(name))
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(
                    f"Required test modules are missing: {name}", result.stderr
                )
                self.assert_no_tests_ran()

    def test_known_gui_suites_are_explicitly_separate(self):
        """Optional graphical hosts neither run nor make strict PTY results skip."""
        for name in GUI_MODULES:
            (self.root / "tests" / f"{name}.py").write_text(
                'raise AssertionError("GUI host must be requested separately")\n'
            )
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        expected = len(ALL_CASES) + len(CONTRACT_MODULES)
        self.assertIn(f"Ran {expected} tests", result.stderr)

    def test_new_gui_name_is_not_silently_excluded(self):
        """Adding a suffix cannot evade required discovery without explicit review."""
        (self.root / "tests/test_terminal_added_gui.py").write_text(PASSING_TEST)
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / "tests/test_terminal_added_gui.ran").is_file())

    def test_skips_fail(self):
        """A skipped native case cannot be reported as successful regression."""
        (self.root / "tests/test_terminal_added.py").write_text(
            PASSING_TEST.replace(
                "    def test_observed_result",
                '    @unittest.skip("fixture unavailable")\n    def test_observed_result',
            )
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAILED (skipped=1)", result.stderr)
        self.assertFalse((self.root / "tests/test_terminal_added.ran").exists())

    def test_empty_module_fails(self):
        """An empty added file does not count as implemented coverage."""
        (self.root / "tests/test_terminal_added.py").write_text("# No tests.\n")
        result = self.run_check()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(
            "Test module collected no tests: test_terminal_added", result.stderr
        )
        self.assert_no_tests_ran()

    def test_import_error_fails(self):
        """Import errors are executed as failing tests, never discarded by discovery."""
        (self.root / "tests/test_terminal_added.py").write_text(
            'raise ImportError("fixture import failed")\n'
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("fixture import failed", result.stderr)
        self.assertIn("FAILED (errors=1)", result.stderr)
        # A mapped module also fails collection before execution; retain the
        # original traceback instead of replacing it with only missing IDs.
        (self.root / "tests/test_terminal_added.py").unlink()
        (self.root / "tests/test_terminal_contracts.py").write_text(
            'raise ImportError("mapped fixture import failed")\n'
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("ImportError: mapped fixture import failed", result.stderr)
        self.assertIn("Required terminal cases missing", result.stderr)

    def test_missing_native_programs_fail(self):
        """PATH executables must exist before unittest can skip their coverage."""
        self.env["PATH"] = str(self.root / "empty-path")
        result = self.run_check()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(
            "Required native programs are missing: bash, zsh, tcsh, vim", result.stderr
        )
        self.assert_no_tests_ran()

    def test_missing_independent_test_programs_fail(self):
        """Missing terminfo/X11 witnesses must not silently reduce test coverage."""
        directory = self.root / "native-only"
        directory.mkdir()
        for name in ("bash", "zsh", "tcsh", "vim"):
            (directory / name).symlink_to(shutil.which(name))
        self.env["PATH"] = str(directory)
        result = self.run_check()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(
            "Required test programs are missing: infocmp, tput, Xvfb, xclip",
            result.stderr,
        )
        self.assert_no_tests_ran()

    def test_missing_library_fails(self):
        """The configured shared object must actually load before the suite runs."""
        self.env["ISH_GHOSTTY_LIB"] = str(self.root / "missing-libish_vt.so")
        result = self.run_check()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Cannot load", result.stderr)
        self.assertIn("missing-libish_vt.so", result.stderr)
        self.assert_no_tests_ran()

    def test_loadable_library_without_bridge_abi_fails(self):
        """A loadable system library is not mistaken for a compatible VT bridge."""
        library = ctypes.util.find_library("c")
        self.assertIsNotNone(library, "the Linux C runtime must be discoverable")
        self.env["ISH_GHOSTTY_LIB"] = library
        result = self.run_check()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Missing native metadata entry point ish_vt_abi", result.stderr)
        self.assert_no_tests_ran()

    def test_incompatible_dependency_fails(self):
        """Installed dependencies must satisfy the project's declared version bounds."""
        project = self.root / "pyproject.toml"
        project.write_text(
            project.read_text().replace(
                "dependencies = []", 'dependencies = ["prompt-toolkit==0.0.0"]'
            )
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("Installed prompt-toolkit", result.stderr)
        self.assertIn("does not satisfy ==0.0.0", result.stderr)
        self.assert_no_tests_ran()
