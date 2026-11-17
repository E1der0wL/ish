"""Pin existing automated evidence to baseline IDs; this is not a pass registry.

Each entry protects specific collected test IDs, not every subcase in the audit
row. Physical input, pixels, unverified host features and optional extensions
remain outside this automatic gate. See docs/VT_TEST_CONTRACTS.md for its scope.
"""

REQUIRED_CHECKS = (
    "test_terminal_checks.CheckCommandTests.test_required_evidence_matches_independent_ids_and_case_names",
    "test_terminal_checks.CheckCommandTests.test_each_mapped_case_is_required_exactly_once_in_the_collected_suite",
    "test_terminal_checks.CheckCommandTests.test_removed_or_renamed_case_fails_with_its_baseline_id",
    "test_terminal_checks.CheckCommandTests.test_load_tests_cannot_hide_an_existing_required_method",
    "test_terminal_checks.CheckCommandTests.test_missing_evidence_mapping_fails_before_test_execution",
)
SHELL_CASES = (
    "test_terminal_hookless.HooklessTests.test_bash_plain",
    "test_terminal_hookless.HooklessTests.test_bash_plugins",
    "test_terminal_hookless.HooklessTests.test_zsh_plain",
    "test_terminal_hookless.HooklessTests.test_zsh_plugins",
    "test_terminal_hookless.HooklessTests.test_tcsh_plain",
    "test_terminal_hookless.HooklessTests.test_tcsh_plugins",
)
REQUIRED_CASES = {
    "FP01": (
        "test_terminal_features.FeaturePolicyTests.test_auto_is_rejected_without_an_implemented_support_query",
        "test_terminal_features.FeaturePolicyTests.test_on_and_off_do_not_rewrite_observed_support",
        "test_terminal_features.FeaturePolicyTests.test_auto_uses_anonymous_replies_and_never_a_version_name",
        "test_terminal_features.FeaturePolicyTests.test_off_survives_geometry_loss_and_recovery",
        "test_terminal_features.FeaturePolicyTests.test_forced_images_still_require_geometry_and_one_output_backend",
        "test_terminal_features.FeaturePolicyTests.test_cursor_on_without_restorable_initial_color_does_not_write",
        "test_terminal_features.FeaturePolicyTests.test_late_duplicate_and_cancelled_replies_cannot_upgrade_auto",
        "test_terminal_composition.FormattingPolicyTests.test_default_formats_reach_native_cells_without_host_identity",
        "test_terminal_composition.FormattingPolicyTests.test_disabled_formats_emit_neither_content_nor_optional_cleanup",
        "test_terminal_composition.FormattingPolicyTests.test_written_cursor_color_restores_even_when_execution_is_disabled",
    ),
    "IO01": SHELL_CASES,
    "IO02": (
        "test_terminal_session.TerminalSessionTests.test_host_writer_backpressure_preserves_final_output",
        "test_terminal_supplements.SustainedOutputTests.test_tcsh_output_survives_popup_and_repeated_resize",
        "test_terminal_supplements.SustainedOutputTests.test_bash_output_survives_popup_and_repeated_resize",
        "test_terminal_supplements.SustainedOutputTests.test_zsh_output_survives_popup_and_repeated_resize",
    ),
    "IO03": (
        "test_terminal_session.TerminalSessionTests.test_paused_exit_with_open_slave_drains_without_waiting_for_eof",
    ),
    "IO04": (
        "test_terminal_cursor.CursorPTYTests.test_child_first_size_already_excludes_selected_bar",
        "test_terminal_vt.TerminalVTTests.test_resize_updates_the_grid_and_native_size_report",
    ),
    "IO05": SHELL_CASES,
    "IO06": (
        "test_terminal_cursor.RestorationPTYTests.test_initial_queries_precede_any_mode_change_and_exit_restores",
        "test_terminal_cursor.RestorationPTYTests.test_term_and_hup_restore_the_same_observed_values",
        "test_terminal_cursor.RestorationPTYTests.test_suspend_restores_and_resume_retains_the_same_native_shell",
        "test_terminal_composition.TerminalCompositionTests.test_failed_final_layout_still_restores_owned_host_metadata",
    ),
    "IO07": (
        "test_terminal_contracts.BaselineContracts.test_processing_failure_cannot_be_reported_as_a_successful_feed",
    ),
    "IO08": SHELL_CASES,
    "VT01": (
        "test_terminal_contracts.BaselineContracts.test_fragmentation_cancellation_and_query_identity",
    ),
    "VT02": (
        "test_terminal_vt.TerminalVTTests.test_split_unicode_and_wide_cells_keep_text_and_cursor",
    ),
    "VT03": (
        "test_terminal_contracts.BaselineContracts.test_forward_backward_and_cleared_tabs_use_physical_columns",
        "test_terminal_contracts.TerminfoContracts.test_cursor_capabilities_reach_the_expected_cell",
    ),
    "VT04": (
        "test_terminal_contracts.BaselineContracts.test_horizontal_margins_limit_wrapping_and_scrolling",
        "test_terminal_contracts.BaselineContracts.test_wrap_disable_and_carriage_return_cancel_pending_wrap",
    ),
    "VT05": (
        "test_terminal_contracts.BaselineContracts.test_insert_delete_repeat_and_erase_are_positioned",
        "test_terminal_contracts.TerminfoContracts.test_edit_capabilities_produce_expected_text",
    ),
    "VT06": (
        "test_terminal_contracts.BaselineContracts.test_region_scroll_reverse_index_and_next_line_keep_outer_rows",
        "test_terminal_contracts.BaselineContracts.test_history_erase_does_not_erase_the_visible_screen",
    ),
    "VT07": (
        "test_terminal_contracts.BaselineContracts.test_alternate_47_1047_and_saved_cursor_1048_have_distinct_effects",
        "test_terminal_contracts.BaselineContracts.test_1049_resize_and_repeated_exit_keep_primary_text",
    ),
    "VT08": (
        "test_terminal_vt.TerminalVTTests.test_default_colors_and_sgr_resets_keep_host_defaults",
        "test_terminal_startup.StartupTests.test_color_depth_preserves_explicit_rgb_and_respects_ptk_environment",
    ),
    "VT09": ("test_terminal_vt.TerminalVTTests.test_cursor_erase_and_rgb_style",),
    "VT11": (
        "test_terminal_cursor.NativeCursorTests.test_native_defaults_wait_for_explicit_reset",
        "test_terminal_composition.TerminalCompositionTests.test_repeated_reset_does_not_overwrite_restored_cursor_modes",
    ),
    "VT12": (
        "test_terminal_contracts.BaselineContracts.test_soft_reset_resets_input_modes_without_erasing_text",
    ),
    "VT13": (
        "test_terminal_contracts.BaselineContracts.test_dec_line_drawing_and_selective_reset",
    ),
    "VT14": (
        "test_terminal_native_features.NativeFeaturesTests.test_hold_captures_exact_color_boundaries_inside_one_write",
    ),
    "VT15": (
        "test_terminal_vt.TerminalVTTests.test_selection_never_uses_anchors_from_another_screen_or_reset",
    ),
    "IN01": (
        "test_terminal_host.HostProbeTests.test_mouse_arrow_cpr_alt_and_lone_escape_keep_ptk_semantics",
        "test_terminal_vt.TerminalVTTests.test_cursor_keys_paste_and_controls_follow_terminal_modes",
    ),
    "IN02": (
        "test_terminal_contracts.TerminfoContracts.test_all_advertised_function_keys_match_native_modifier_encoding",
        "test_terminal_contracts.TerminfoContracts.test_advertised_navigation_matches_application_cursor_mode",
        "test_terminal_contracts.TerminfoContracts.test_all_advertised_modified_navigation_matches_native_encoding",
        "test_terminal_contracts.TerminfoContracts.test_all_advertised_keypad_entries_match_application_keypad_mode",
        "test_terminal_contracts.TerminfoContracts.test_keypad_separator_obeys_native_modes_modifiers_and_event_actions",
        "test_terminal_contracts.LiveQueryContracts.test_three_shells_return_queries_and_keys_to_the_native_reader",
    ),
    "IN04": (
        "test_terminal_host.HostInputTests.test_real_reader_preserves_paste_as_one_event",
    ),
    "IN06": (
        "test_terminal_composition.TerminalCompositionTests.test_closing_popup_while_host_unfocused_never_claims_native_focus",
    ),
    "IN07": (
        "test_terminal_mouse_policy.MousePolicyTests.test_auto_mouse_reaches_actual_pty_consumer_and_file_once",
        "test_terminal_cli.TerminalCLITests.test_vim_mouse_changes_cursor_and_keeps_primary_history_separate",
    ),
    "IN09": (
        "test_terminal_mouse_policy.MousePolicyTests.test_default_and_keyboard_surfaces_leave_host_selection_available",
        "test_terminal_selection.SelectionTests.test_single_double_and_triple_click_use_native_word_and_line_ranges",
    ),
    "IN10": (
        "test_terminal_vt.TerminalVTTests.test_alternate_screen_does_not_borrow_primary_history",
    ),
    "IN12": (
        "test_terminal_contracts.SearchContracts.test_history_search_selects_and_reveals_matches",
        "test_terminal_composition.TerminalCompositionTests.test_selected_search_popup_routes_real_keys_to_native_history",
    ),
    "IN13": (
        "test_terminal_composition.TerminalCompositionTests.test_popup_input_focus_results_and_stale_completion",
    ),
    "HX01": (
        "test_terminal_contracts.LiveQueryContracts.test_three_shells_return_queries_and_keys_to_the_native_reader",
    ),
    "HX02": (
        "test_terminal_contracts.BaselineContracts.test_fragmentation_cancellation_and_query_identity",
        "test_terminal_contracts.TerminfoContracts.test_all_advertised_function_keys_match_native_modifier_encoding",
        "test_terminal_contracts.TerminfoContracts.test_all_advertised_modified_navigation_matches_native_encoding",
        "test_terminal_contracts.TerminfoContracts.test_all_advertised_keypad_entries_match_application_keypad_mode",
        "test_terminal_contracts.TerminfoContracts.test_keypad_separator_obeys_native_modes_modifiers_and_event_actions",
        "test_terminal_contracts.TerminfoContracts.test_saved_cursor_tabs_wrap_and_screen_clear_use_database_bytes",
    ),
    "HX03": (
        "test_terminal_contracts.BaselineContracts.test_queries_report_only_virtual_modes_cursor_and_sizes",
    ),
    "HX04": (
        "test_terminal_native_features.NativeFeaturesTests.test_palette_updates_preserve_child_override_until_osc_reset",
    ),
    "HX06": (
        "test_terminal_composition.TerminalCompositionTests.test_titles_are_published_without_host_identification_and_retained",
    ),
    "HX08": (
        "test_terminal_clipboard.ClipboardTests.test_unverified_host_and_disabled_policy_never_claim_success",
        "test_terminal_clipboard.ClipboardTests.test_timeout_and_wrong_identity_cannot_complete_a_later_request",
        "test_terminal_clipboard_provider.ClipboardProviderTests.test_default_write_policy_and_explicit_read_return_fresh_text",
        "test_terminal_clipboard_provider.ClipboardProviderTests.test_reset_rejects_late_completion_and_old_timeout_during_new_read",
        "test_terminal_clipboard_provider.ClipboardProviderTests.test_close_cancels_read_and_suppresses_late_reply",
        "test_terminal_clipboard_provider.ClipboardProviderTests.test_consecutive_osc52_copy_and_read_preserve_order_without_yielding",
        "test_terminal_clipboard_provider.ClipboardProviderTests.test_idle_waits_for_all_queued_copies_before_child_exit",
        "test_terminal_clipboard_provider.ClipboardProviderTests.test_response_sink_failure_reaches_application_failure_boundary",
        "test_terminal_clipboard_provider.X11ProviderTests.test_independent_unicode_empty_and_incremental_roundtrip",
        "test_terminal_clipboard_provider.X11ProviderTests.test_actual_vt_and_controlling_pty_return_provider_data_to_foreground_file",
    ),
    "HX12": (
        "test_terminal_startup.StartupTests.test_diagnose_explains_unsupported_controls_without_claiming_host_support",
    ),
    "HX13": (
        "test_terminal_contracts.BaselineContracts.test_native_diagnostics_do_not_dereference_or_capture_payloads",
    ),
    "UI01": (
        "test_terminal_composition.TerminalCompositionTests.test_failed_final_layout_still_restores_owned_host_metadata",
    ),
    "UI02": (
        "test_terminal_composition.TerminalCompositionTests.test_default_is_full_native_grid_without_surfaces_or_reserved_keys",
    ),
    "UI03": (
        "test_terminal_cursor.CursorPTYTests.test_child_first_size_already_excludes_selected_bar",
        "test_terminal_composition.TerminalCompositionTests.test_bar_always_leaves_a_native_row_when_outer_terminal_shrinks",
    ),
    "UI04": (
        "test_terminal_composition.TerminalCompositionTests.test_popup_covering_either_half_of_wide_grapheme_restores_it",
    ),
    "UI05": (
        "test_terminal_composition.TerminalCompositionTests.test_async_plugin_failure_diagnoses_and_restores_focus",
    ),
    "UI06": (
        "test_terminal_startup.StartupTests.test_queries_do_not_execute_rc_plugins_shell_or_native_library",
        "test_terminal_startup.StartupTests.test_language_is_cli_then_lang_then_builtin_english",
        "test_terminal_startup.StartupTests.test_saved_vt_plugins_persist_without_loading_stream_plugins",
        "test_terminal_startup.StartupTests.test_selected_file_logs_exclude_native_payload_and_obey_level",
    ),
    "UI07": (
        "test_terminal_startup.StartupTests.test_help_and_diagnostics_distinguish_vt_from_stream",
        "test_terminal_vt.TerminalVTTests.test_zero_byte_limit_erases_and_disables_native_history",
    ),
    "UI08": (
        "test_terminal_cursor.RestorationPTYTests.test_origin_restoration_keeps_the_primary_cursor_and_scroll_region",
    ),
    "IM08": (
        "test_terminal_startup.StartupTests.test_image_rejections_are_counted_without_retaining_payload_in_logs",
    ),
    "EXT01": (
        "test_terminal_supplements.SharedDirectoryTests.test_one_directory_loads_output_keys_and_clipboard_without_ui",
        "test_terminal_keys.KeyMappingTests.test_reserved_keys_responses_and_protocols_cannot_be_mapped",
        "test_terminal_keys.KeyMappingTests.test_registration_limits_and_first_input_seal",
        "test_terminal_keys.KeyMappingTests.test_incomplete_and_oversized_csi_preserve_control_keys_and_following_text",
        "test_terminal_keys.InputProcessorResponseTests.test_batched_cursor_report_preserves_adjacent_prefix_keys",
        "test_terminal_keys.InputProcessorResponseTests.test_individual_cursor_report_preserves_pending_prefix",
        "test_terminal_keys.InputProcessorResponseTests.test_reinserted_batch_filters_only_report_and_preserves_order",
        "test_terminal_keys.KeyMappingPTYTests.test_sample_plugin_keys_follow_popup_focus_and_actual_pty_consumer",
        "test_terminal_keys.KeyMappingPTYTests.test_unknown_responses_never_leak_to_stdin_and_paste_keeps_native_policy",
        "test_terminal_terminfo.RemainingTerminfoContracts.test_advertised_inventory_has_no_unreviewed_entries",
    ),
}
