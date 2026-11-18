/* Narrow, bounded ABI for ish's libghostty-vt 1.0 prototype. */
#include <ghostty/vt.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

#ifndef ISH_BRIDGE_ABI
#error "Build with tools/build_ghostty_vt.py to select the private ABI"
#endif

#define ISH_MAX_CELLS 262144u
#define ISH_MAX_BUFFER (32u * 1024u * 1024u)
#define ISH_LIMIT (-100)
#define ISH_MEMORY (-101)
#define ISH_PROCESSING (-102)
#define ISH_SEARCH_STEPS 64u
#define ISH_SEARCH_BYTES 4096u
#define ISH_IDENTITY_BYTES 128u
#define ISH_COLOR_FG 1u
#define ISH_COLOR_BG 2u
#define ISH_KITTY_PLACEHOLDER 0x10EEEEu
#define ISH_APPEARANCE_BYTES (259u * 4u)
#define ISH_NOTIFICATION_BYTES (16u * 1024u)
#define ISH_NOTIFICATIONS_PER_DRAIN 64u
/* Logical gesture units, not a fabricated host pixel measurement. Subcells
 * preserve native within-cell thresholds without its one-unit edge buffer
 * turning the entire first/last text row into an autoscroll zone. */
#define ISH_SELECTION_UNITS 1024u
#define CHECK(call) do { int status = (call); if (status != GHOSTTY_SUCCESS) return status; } while (0)

static const GhosttyMouseAction MOUSE_ACTIONS[] = {
    GHOSTTY_MOUSE_ACTION_PRESS, GHOSTTY_MOUSE_ACTION_RELEASE, GHOSTTY_MOUSE_ACTION_MOTION
};
static const GhosttyMouseButton MOUSE_BUTTONS[] = {
    GHOSTTY_MOUSE_BUTTON_UNKNOWN, GHOSTTY_MOUSE_BUTTON_LEFT, GHOSTTY_MOUSE_BUTTON_MIDDLE,
    GHOSTTY_MOUSE_BUTTON_RIGHT, GHOSTTY_MOUSE_BUTTON_FOUR, GHOSTTY_MOUSE_BUTTON_FIVE,
    GHOSTTY_MOUSE_BUTTON_SIX, GHOSTTY_MOUSE_BUTTON_SEVEN
};
/* xterm's keypad separator, indexed by the bridge SHIFT/CTRL/ALT bits.
 * libghostty 1.0.1 has the enhanced key identity but no legacy table entry. */
static const char KEYPAD_SEPARATOR_TEXT[] = ",";
static const char *KEYPAD_SEPARATOR_APP[] = {
    "\x1bOl", "\x1bO2l", "\x1bO5l", "\x1bO6l",
    "\x1bO3l", "\x1bO4l", "\x1bO7l", "\x1bO8l"
};

/* These are dependencies of this bridge, not an advertisement of all the
 * features present in libghostty. Stripped builds must fail before allocation. */
static const struct {
    GhosttyBuildInfo field;
    const char *name;
} REQUIRED_FEATURES[] = {
    {GHOSTTY_BUILD_INFO_KITTY_GRAPHICS, "kitty_graphics"},
    {GHOSTTY_BUILD_INFO_FEATURE_FORMATTER, "formatter"},
    {GHOSTTY_BUILD_INFO_FEATURE_SELECTION, "selection"},
    {GHOSTTY_BUILD_INFO_FEATURE_SEARCH, "search"},
    {GHOSTTY_BUILD_INFO_FEATURE_RENDER_STATE, "render_state"},
    {GHOSTTY_BUILD_INFO_FEATURE_INPUT_ENCODE, "input_encode"},
    {GHOSTTY_BUILD_INFO_FEATURE_COLOR, "color"},
    {GHOSTTY_BUILD_INFO_FEATURE_GRID_INTROSPECTION, "grid_introspection"},
};

uint32_t ish_vt_abi(void) { return ISH_BRIDGE_ABI; }
const char *ish_vt_headers(void) { return GHOSTTY_VT_VERSION; }
const char *ish_vt_version(void) { return ghostty_version(); }
const char *ish_vt_missing_feature(void) {
    for (size_t i = 0; i < sizeof(REQUIRED_FEATURES) / sizeof(REQUIRED_FEATURES[0]); i++) {
        bool enabled = false;
        if (ghostty_build_info(REQUIRED_FEATURES[i].field, &enabled) != GHOSTTY_SUCCESS || !enabled)
            return REQUIRED_FEATURES[i].name;
    }
    return NULL;
}

typedef struct { uint8_t *data; size_t len, cap; int error; } Buffer;
typedef struct {
    GhosttyTerminal terminal;
    GhosttyRenderState render;
    GhosttyRenderStateRowIterator rows;
    GhosttyRenderStateRowCells cells;
    GhosttyRenderStateImageIterator images;
    GhosttyKeyEncoder encoder;
    GhosttyKeyEvent event;
    GhosttyMouseEncoder mouse_encoder;
    GhosttyMouseEvent mouse_event;
    GhosttyFormatter formatter;
    GhosttySearch search;
    GhosttySelectionGesture gesture;
    GhosttyTrackedGridRef anchor;
    GhosttyTerminalScreen anchor_screen;
    GhosttyTrackedGridRef last_output;
    GhosttyTerminalScreen output_screen;
    unsigned notifications;
    Buffer output, replies, grapheme, link, effects, identity;
    Buffer held_frame, held_images;
    bool held;
    uint64_t hold_serial;
    int hold_error;
    uint16_t cols, height;
    uint32_t cell_width, cell_height;
    uint32_t adapter_image_id;
    int scheme;
    bool colors_dirty;
    bool sixel_supported;
    GhosttyColorRgb color_overrides[3];
    int color_results[3];
} Terminal;

static int capture_hold(Terminal *state);
static void release_hold(Terminal *state) {
    state->held = false;
    state->held_frame.len = state->held_images.len = 0;
}
static void render_hold(GhosttyTerminal terminal, void *userdata, bool held) {
    (void)terminal;
    Terminal *state = userdata;
    if (!held) { release_hold(state); return; }
    state->hold_serial++;
    state->colors_dirty = true;
    state->hold_error = capture_hold(state);
    state->held = state->hold_error == 0;
}

static int reserve(Buffer *buffer, size_t extra) {
    if (extra > ISH_MAX_BUFFER - buffer->len) return ISH_LIMIT;
    size_t needed = buffer->len + extra;
    if (needed <= buffer->cap) return 0;
    size_t capacity = buffer->cap ? buffer->cap : 4096;
    while (capacity < needed) capacity *= 2;
    uint8_t *data = realloc(buffer->data, capacity);
    if (!data) return ISH_MEMORY;
    buffer->data = data; buffer->cap = capacity;
    return 0;
}
static int append(Buffer *buffer, const void *data, size_t len) {
    CHECK(reserve(buffer, len));
    if (len) memcpy(buffer->data + buffer->len, data, len);
    buffer->len += len;
    return 0;
}
static void put32(uint8_t *out, uint32_t value) {
    for (unsigned i = 0; i < 4; i++) out[i] = (uint8_t)(value >> (8 * i));
}
static int number(Buffer *buffer, uint32_t value) {
    uint8_t bytes[4]; put32(bytes, value); return append(buffer, bytes, 4);
}
static void write_pty(GhosttyTerminal terminal, void *userdata, const uint8_t *data, size_t len) {
    (void)terminal;
    Terminal *state = userdata;
    if (!state->replies.error) state->replies.error = append(&state->replies, data, len);
}
static bool report_size(GhosttyTerminal terminal, void *userdata, GhosttySizeReportSize *size) {
    (void)terminal;
    Terminal *state = userdata;
    size->columns = state->cols; size->rows = state->height;
    size->cell_width = state->cell_width; size->cell_height = state->cell_height;
    return true;
}
static bool report_scheme(GhosttyTerminal terminal, void *userdata, GhosttyColorScheme *scheme) {
    (void)terminal;
    Terminal *state = userdata;
    if (state->scheme < 0) return false;
    *scheme = state->scheme;
    return true;
}
static void effect(Terminal *state, uint32_t kind, const uint8_t *data, size_t len) {
    Buffer *buffer = &state->effects;
    if (buffer->error) return;
    buffer->error = reserve(buffer, 8 + len);
    if (buffer->error) return;
    number(buffer, kind); number(buffer, (uint32_t)len); append(buffer, data, len);
}
static void bell(GhosttyTerminal terminal, void *userdata) {
    (void)terminal; effect(userdata, 1, NULL, 0);
}
static void title_changed(GhosttyTerminal terminal, void *userdata) {
    GhosttyString title = {0};
    if (!ghostty_terminal_get(terminal, GHOSTTY_TERMINAL_DATA_TITLE, &title))
        effect(userdata, 2, title.ptr, title.len);
}
static void pwd_changed(GhosttyTerminal terminal, void *userdata) {
    GhosttyString pwd = {0};
    if (!ghostty_terminal_get(terminal, GHOSTTY_TERMINAL_DATA_PWD, &pwd))
        effect(userdata, 3, pwd.ptr, pwd.len);
}
static void desktop_notification(GhosttyTerminal terminal, void *userdata,
                                 const GhosttyTerminalDesktopNotification *notification) {
    (void)terminal;
    Terminal *state = userdata;
    if (notification->title.len > ISH_NOTIFICATION_BYTES ||
        notification->body.len > ISH_NOTIFICATION_BYTES - notification->title.len ||
        state->notifications >= ISH_NOTIFICATIONS_PER_DRAIN) return;
    Buffer *buffer = &state->effects;
    size_t length = 4 + notification->title.len + notification->body.len;
    if (buffer->error) return;
    buffer->error = reserve(buffer, length + 8);
    if (buffer->error) return;
    number(buffer, 4); number(buffer, (uint32_t)length);
    number(buffer, (uint32_t)notification->title.len);
    append(buffer, notification->title.ptr, notification->title.len);
    append(buffer, notification->body.ptr, notification->body.len);
    state->notifications++;
}
static void reset(GhosttyTerminal terminal, void *userdata) {
    (void)terminal;
    Terminal *state = userdata;
    ghostty_tracked_grid_ref_free(state->last_output);
    state->last_output = NULL;
}
static void semantic_prompt(GhosttyTerminal terminal, void *userdata,
                            const GhosttyTerminalSemanticPrompt *prompt) {
    if (prompt->kind != GHOSTTY_SEMANTIC_PROMPT_OUTPUT_START) return;
    Terminal *state = userdata;
    reset(terminal, state);
    uint16_t x = 0, y = 0;
    int status = ghostty_terminal_get(terminal, GHOSTTY_TERMINAL_DATA_CURSOR_X, &x);
    if (!status) status = ghostty_terminal_get(terminal, GHOSTTY_TERMINAL_DATA_CURSOR_Y, &y);
    if (!status) status = ghostty_terminal_get(terminal, GHOSTTY_TERMINAL_DATA_ACTIVE_SCREEN, &state->output_screen);
    GhosttyPoint point = {.tag = GHOSTTY_POINT_TAG_ACTIVE, .value = {.coordinate = {.x = x, .y = y}}};
    if (!status) status = ghostty_terminal_grid_ref_track(terminal, point, &state->last_output);
    if (status && !state->effects.error) state->effects.error = status;
}
int ish_vt_png_decoder(GhosttySysDecodePngFn callback) {
    return ghostty_sys_set(GHOSTTY_SYS_OPT_DECODE_PNG, (const void *)callback);
}
int ish_vt_log(GhosttySysLogFn callback) {
    return ghostty_sys_set(GHOSTTY_SYS_OPT_LOG, (const void *)callback);
}
static GhosttyString xtversion(GhosttyTerminal terminal, void *userdata) {
    (void)terminal;
    Terminal *state = userdata;
    return (GhosttyString){.ptr = state->identity.data, .len = state->identity.len};
}
int ish_vt_identity(Terminal *state, const uint8_t *name, size_t name_len,
                    const uint8_t *version, size_t version_len) {
    if (!state || !name || !version || name_len > ISH_IDENTITY_BYTES || version_len > ISH_IDENTITY_BYTES) return ISH_LIMIT;
    GhosttyString term = {.ptr = name, .len = name_len};
    CHECK(ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_TERMINFO_NAME, &term));
    state->identity.len = 0;
    CHECK(append(&state->identity, version, version_len));
    return ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_XTVERSION, (const void *)xtversion);
}
void *ish_vt_image_alloc(const GhosttyAllocator *allocator, size_t size) {
    return size <= ISH_MAX_BUFFER ? ghostty_alloc(allocator, size) : NULL;
}
static bool device_attributes(GhosttyTerminal terminal, void *userdata, GhosttyDeviceAttributes *attrs) {
    (void)terminal;
    Terminal *state = userdata;
    attrs->primary.conformance_level = GHOSTTY_DA_CONFORMANCE_VT220;
    attrs->primary.features[0] = GHOSTTY_DA_FEATURE_ANSI_COLOR;
    attrs->primary.num_features = 1;
    if (state->sixel_supported)
        attrs->primary.features[attrs->primary.num_features++] = GHOSTTY_DA_FEATURE_SIXEL;
    attrs->secondary.device_type = GHOSTTY_DA_DEVICE_TYPE_VT220;
    return true;
}
static int dimensions(unsigned cols, unsigned rows) {
    return cols && rows && cols <= UINT16_MAX && rows <= UINT16_MAX &&
        (uint64_t)cols * rows <= ISH_MAX_CELLS ? 0 : ISH_LIMIT;
}
void ish_vt_free(Terminal *state) {
    if (!state) return;
    ghostty_tracked_grid_ref_free(state->anchor);
    ghostty_tracked_grid_ref_free(state->last_output);
    ghostty_selection_gesture_free(state->gesture, state->terminal);
    ghostty_formatter_free(state->formatter);
    ghostty_search_free(state->search);
    ghostty_mouse_event_free(state->mouse_event);
    ghostty_mouse_encoder_free(state->mouse_encoder);
    ghostty_key_event_free(state->event);
    ghostty_key_encoder_free(state->encoder);
    ghostty_render_state_row_cells_free(state->cells);
    ghostty_render_state_image_iterator_free(state->images);
    ghostty_render_state_row_iterator_free(state->rows);
    ghostty_render_state_free(state->render);
    ghostty_terminal_free(state->terminal);
    free(state->output.data); free(state->replies.data); free(state->grapheme.data);
    free(state->link.data); free(state->effects.data); free(state->identity.data);
    free(state->held_frame.data); free(state->held_images.data);
    free(state);
}
Terminal *ish_vt_new(unsigned cols, unsigned rows) {
    if (dimensions(cols, rows)) return NULL;
    Terminal *state = calloc(1, sizeof(*state));
    if (!state) return NULL;
    state->cols = cols; state->height = rows;
    state->scheme = -1; state->colors_dirty = true;
    if (ghostty_terminal_new(NULL, &state->terminal, cols, rows) ||
        ghostty_render_state_new(NULL, &state->render) ||
        ghostty_render_state_row_iterator_new(NULL, &state->rows) ||
        ghostty_render_state_row_cells_new(NULL, &state->cells) ||
        ghostty_render_state_image_iterator_new(NULL, &state->images) ||
        ghostty_key_encoder_new(NULL, &state->encoder) ||
        ghostty_key_event_new(NULL, &state->event) ||
        ghostty_mouse_encoder_new(NULL, &state->mouse_encoder) ||
        ghostty_mouse_event_new(NULL, &state->mouse_event) ||
        ghostty_selection_gesture_new(NULL, &state->gesture) ||
        ghostty_formatter_terminal_new(NULL, &state->formatter, state->terminal) ||
        ghostty_search_new(NULL, &state->search, state->terminal) ||
        ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_USERDATA, state) ||
        ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_WRITE_PTY, (const void *)write_pty) ||
        ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_SIZE, (const void *)report_size) ||
        ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_COLOR_SCHEME, (const void *)report_scheme) ||
        ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_BELL, (const void *)bell) ||
        ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_TITLE_CHANGED, (const void *)title_changed) ||
        ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_PWD_CHANGED, (const void *)pwd_changed) ||
        ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_DESKTOP_NOTIFICATION, (const void *)desktop_notification) ||
        ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_SEMANTIC_PROMPT, (const void *)semantic_prompt) ||
        ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_RESET, (const void *)reset) ||
        ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_RENDER_HOLD, (const void *)render_hold) ||
        ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_DEVICE_ATTRIBUTES, (const void *)device_attributes)) {
        ish_vt_free(state); return NULL;
    }
    bool unwrap = true, trim = false;
    uint64_t image_limit = 0;
    if (ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_KITTY_IMAGE_STORAGE_LIMIT, &image_limit)) {
        ish_vt_free(state); return NULL;
    }
    if (ghostty_formatter_set(state->formatter, GHOSTTY_FORMATTER_OPT_UNWRAP, &unwrap) ||
        ghostty_formatter_set(state->formatter, GHOSTTY_FORMATTER_OPT_TRIM, &trim)) {
        ish_vt_free(state); return NULL;
    }
    return state;
}
int ish_vt_feed(Terminal *state, const uint8_t *data, size_t len) {
    if (!state || (!data && len)) return ISH_LIMIT;
    state->colors_dirty = true;
    ghostty_terminal_vt_write(state->terminal, data, len);
    state->colors_dirty = true;
    bool failed = false;
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_VT_PROCESSING_ERROR, &failed));
    if (failed) return ISH_PROCESSING;
    return state->replies.error ? state->replies.error : state->hold_error;
}
int ish_vt_processing_error(Terminal *state) {
    if (!state) return ISH_LIMIT;
    bool failed = false;
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_VT_PROCESSING_ERROR, &failed));
    return failed;
}
int ish_vt_keyboard_flags(Terminal *state, uint32_t *out) {
    if (!state || !out) return ISH_LIMIT;
    GhosttyKittyKeyFlags flags = 0;
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_KITTY_KEYBOARD_FLAGS, &flags));
    *out = flags;
    return 0;
}
int ish_vt_default_cursor(Terminal *state, unsigned shape, bool blink) {
    if (!state || shape > GHOSTTY_CURSOR_STYLE_BLOCK_HOLLOW) return ISH_LIMIT;
    GhosttyCursorStyle style = (GhosttyCursorStyle)shape;
    CHECK(ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_DEFAULT_CURSOR_STYLE, &style));
    return ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_DEFAULT_CURSOR_BLINK, &blink);
}
int ish_vt_search_set(Terminal *state, const uint8_t *text, size_t len) {
    if (!state || (!text && len) || len > ISH_SEARCH_BYTES) return ISH_LIMIT;
    GhosttyString needle = {.ptr = text, .len = len};
    return ghostty_search_set(state->search, GHOSTTY_SEARCH_OPT_NEEDLE, &needle);
}
int ish_vt_search_step(Terminal *state, unsigned budget, unsigned *status, uint64_t *count) {
    if (!state || !status || !count || !budget || budget > ISH_SEARCH_STEPS) return ISH_LIMIT;
    GhosttySearchStatus progress;
    CHECK(ghostty_search_feed(state->search));
    for (unsigned i = 0; i < budget; i++) {
        CHECK(ghostty_search_tick(state->search, &progress));
        if (progress == GHOSTTY_SEARCH_STATUS_COMPLETE) break;
        if (progress == GHOSTTY_SEARCH_STATUS_FEED_REQUIRED) CHECK(ghostty_search_feed(state->search));
    }
    size_t found;
    CHECK(ghostty_search_get(state->search, GHOSTTY_SEARCH_DATA_TOTAL_MATCHES, &found));
    *status = progress; *count = found;
    return 0;
}
int ish_vt_search_select(Terminal *state, bool previous, uint64_t *index) {
    if (!state || !index) return ISH_LIMIT;
    *index = UINT64_MAX;
    int result = previous ? ghostty_search_select_prev(state->search) : ghostty_search_select_next(state->search);
    if (result == GHOSTTY_NO_VALUE) return 0;
    CHECK(result);
    GhosttySelection selection;
    size_t selected;
    CHECK(ghostty_search_get(state->search, GHOSTTY_SEARCH_DATA_SELECTED_MATCH, &selection));
    CHECK(ghostty_search_get(state->search, GHOSTTY_SEARCH_DATA_SELECTED_INDEX, &selected));
    CHECK(ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_SELECTION, &selection));
    *index = selected;
    return 0;
}
int ish_vt_appearance(Terminal *state, const uint8_t *colors, size_t len, int scheme) {
    if (!state || !colors || len != ISH_APPEARANCE_BYTES || scheme < -1 || scheme > 1) return ISH_LIMIT;
    const GhosttyTerminalOption options[] = {
        GHOSTTY_TERMINAL_OPT_COLOR_FOREGROUND, GHOSTTY_TERMINAL_OPT_COLOR_BACKGROUND,
        GHOSTTY_TERMINAL_OPT_COLOR_CURSOR
    };
    for (size_t i = 0; i < 259; i++) if (colors[i * 4] > 1) return ISH_LIMIT;
    state->colors_dirty = true;
    for (size_t i = 0; i < 3; i++) {
        const uint8_t *entry = colors + i * 4;
        GhosttyColorRgb color = {entry[1], entry[2], entry[3]};
        CHECK(ghostty_terminal_set(state->terminal, options[i], entry[0] ? &color : NULL));
    }
    /* changeDefault preserves the child's OSC palette overrides. Unknown host
     * entries use the library baseline, never stale observations from a theme. */
    CHECK(ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_COLOR_PALETTE, NULL));
    GhosttyColorRgb palette[256];
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_COLOR_PALETTE_DEFAULT, palette));
    for (size_t i = 0; i < 256; i++) {
        const uint8_t *entry = colors + (i + 3) * 4;
        if (entry[0]) palette[i] = (GhosttyColorRgb){entry[1], entry[2], entry[3]};
    }
    CHECK(ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_COLOR_PALETTE, palette));
    int old_scheme = state->scheme;
    state->scheme = scheme;
    if (scheme >= 0 && scheme != old_scheme) {
        GhosttyTerminalModeConfig mode = {.mode = GHOSTTY_MODE_REPORT_COLOR_SCHEME};
        CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_MODE, &mode));
        if (mode.value) {
            uint8_t report[32]; size_t written;
            CHECK(ghostty_color_scheme_report_encode(scheme, report, sizeof(report), &written));
            CHECK(append(&state->replies, report, written));
        }
    }
    return 0;
}
int ish_vt_resize(Terminal *state, unsigned cols, unsigned rows, unsigned cell_width, unsigned cell_height) {
    if (!state) return ISH_LIMIT;
    CHECK(dimensions(cols, rows));
    if (cell_width > UINT16_MAX || cell_height > UINT16_MAX || (!!cell_width != !!cell_height)) return ISH_LIMIT;
    uint16_t old_cols = state->cols, old_rows = state->height;
    uint32_t old_width = state->cell_width, old_height = state->cell_height;
    state->cols = cols; state->height = rows;
    state->cell_width = cell_width; state->cell_height = cell_height;
    int result = ghostty_terminal_resize(state->terminal, cols, rows, cell_width, cell_height);
    if (result) {
        state->cols = old_cols; state->height = old_rows;
        state->cell_width = old_width; state->cell_height = old_height;
    }
    return result;
}
int ish_vt_replies(Terminal *state, const uint8_t **out, size_t *len) {
    if (!state || !out || !len) return ISH_LIMIT;
    *out = state->replies.data; *len = state->replies.len;
    state->replies.len = 0;
    int error = state->replies.error; state->replies.error = 0;
    return error;
}
int ish_vt_effects(Terminal *state, const uint8_t **out, size_t *len) {
    if (!state || !out || !len) return ISH_LIMIT;
    *out = state->effects.data; *len = state->effects.len;
    state->effects.len = 0;
    state->notifications = 0;
    int error = state->effects.error; state->effects.error = 0;
    return error;
}
int ish_vt_graphics(Terminal *state, uint64_t limit) {
    if (!state || limit > ISH_MAX_BUFFER) return ISH_LIMIT;
    return ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_KITTY_IMAGE_STORAGE_LIMIT, &limit);
}
int ish_vt_sixel_supported(Terminal *state, bool enabled) {
    if (!state) return ISH_LIMIT;
    state->sixel_supported = enabled;
    return 0;
}
int ish_vt_image_context(Terminal *state, const uint8_t **out, size_t *len) {
    if (!state || !out || !len) return ISH_LIMIT;
    uint16_t x, y;
    bool pending, ground;
    GhosttyStyle style;
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_CURSOR_X, &x));
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_CURSOR_Y, &y));
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_CURSOR_PENDING_WRAP, &pending));
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_VT_GROUND, &ground));
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_CURSOR_SGR_STYLE, &style));
    Buffer *buffer = &state->output; buffer->len = 0;
    CHECK(number(buffer, state->cols)); CHECK(number(buffer, state->height));
    CHECK(number(buffer, x)); CHECK(number(buffer, y));
    CHECK(number(buffer, state->cell_width)); CHECK(number(buffer, state->cell_height));
    const GhosttyMode modes[] = {GHOSTTY_MODE_ORIGIN, GHOSTTY_MODE_WRAPAROUND,
        GHOSTTY_MODE_INSERT, GHOSTTY_MODE_ENABLE_LEFT_AND_RIGHT_MARGIN};
    for (size_t i = 0; i < 4; i++) {
        GhosttyTerminalModeConfig mode = {.mode = modes[i]};
        CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_MODE, &mode));
        CHECK(number(buffer, mode.value));
    }
    CHECK(number(buffer, pending)); CHECK(number(buffer, ground));
    CHECK(append(buffer, &style, sizeof(style)));
    *out = buffer->data; *len = buffer->len;
    return 0;
}
int ish_vt_unused_image_id(Terminal *state, uint32_t *out) {
    if (!state || !out) return ISH_LIMIT;
    GhosttyKittyGraphics graphics;
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_KITTY_GRAPHICS, &graphics));
    if (!graphics) return GHOSTTY_UNSUPPORTED;
    for (unsigned attempt = 0; attempt < 65536; attempt++) {
        if (!state->adapter_image_id) state->adapter_image_id = 0xFFFFFFu;
        uint32_t candidate = state->adapter_image_id--;
        if (!ghostty_kitty_graphics_image(graphics, candidate)) {
            *out = candidate; return 0;
        }
    }
    return ISH_LIMIT;
}
int ish_vt_release_hold(Terminal *state) {
    if (!state) return ISH_LIMIT;
    GhosttyTerminalModeConfig mode = {.mode = GHOSTTY_MODE_SYNCHRONIZED_OUTPUT, .value = false};
    CHECK(ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_MODE, &mode));
    /* Programmatic mode changes deliberately do not call render_hold. */
    release_hold(state);
    return 0;
}
int ish_vt_focus(Terminal *state, unsigned focused, const uint8_t **out, size_t *len) {
    if (!state || !out || !len) return ISH_LIMIT;
    *out = NULL; *len = 0;
    GhosttyTerminalModeConfig mode = {.mode = GHOSTTY_MODE_FOCUS_EVENT};
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_MODE, &mode));
    if (!mode.value) return 0;
    Buffer *buffer = &state->output; buffer->len = 0;
    CHECK(reserve(buffer, 16));
    CHECK(ghostty_focus_encode(focused ? GHOSTTY_FOCUS_GAINED : GHOSTTY_FOCUS_LOST, buffer->data, buffer->cap, &buffer->len));
    *out = buffer->data; *len = buffer->len;
    return 0;
}

/* Live state is queried from the VT, never reconstructed from output text. */
int ish_vt_state(Terminal *state, uint64_t *out) {
    if (!state || !out) return ISH_LIMIT;
    bool viewport_active, mouse_tracking;
    GhosttyTerminalScreen screen;
    GhosttyTerminalModeConfig alternate_scroll = {.mode = GHOSTTY_MODE_MOUSE_ALTERNATE_SCROLL};
    GhosttyTerminalModeConfig synchronized = {.mode = GHOSTTY_MODE_SYNCHRONIZED_OUTPUT};
    GhosttyTerminalScrollbar scrollbar;
    size_t history;
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_VIEWPORT_ACTIVE, &viewport_active));
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_ACTIVE_SCREEN, &screen));
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_MODE, &alternate_scroll));
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_MODE, &synchronized));
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_MOUSE_TRACKING, &mouse_tracking));
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_SCROLLBACK_ROWS, &history));
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_SCROLLBAR, &scrollbar));
    out[0] = viewport_active; out[1] = screen; out[2] = alternate_scroll.value;
    out[3] = mouse_tracking; out[4] = history; out[5] = scrollbar.offset;
    out[6] = synchronized.value;
    out[7] = state->hold_serial;
    return 0;
}
int ish_vt_scrollback_limit(Terminal *state, uint64_t lines, uint64_t bytes) {
    if (!state || lines > SIZE_MAX || bytes > SIZE_MAX) return ISH_LIMIT;
    size_t line_limit = (size_t)lines, byte_limit = (size_t)bytes;
    CHECK(ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_SCROLLBACK_MAX_BYTES, &byte_limit));
    return ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_SCROLLBACK_MAX_LINES, &line_limit);
}
int ish_vt_scroll(Terminal *state, unsigned kind, int64_t rows) {
    if (!state || kind > 3 || rows > INTPTR_MAX || rows < INTPTR_MIN) return ISH_LIMIT;
    GhosttyTerminalScrollViewport behavior = {0};
    behavior.tag = kind == 0 ? GHOSTTY_TERMINAL_SCROLL_VIEWPORT_TOP :
        kind == 1 ? GHOSTTY_TERMINAL_SCROLL_VIEWPORT_BOTTOM :
        kind == 2 ? GHOSTTY_TERMINAL_SCROLL_VIEWPORT_DELTA : GHOSTTY_TERMINAL_SCROLL_VIEWPORT_DELTA_PROMPT;
    behavior.value.delta = (intptr_t)rows;
    return ghostty_terminal_scroll_viewport(state->terminal, behavior);
}
static int number64(Buffer *buffer, uint64_t value) {
    uint8_t bytes[8];
    for (unsigned i = 0; i < 8; i++) bytes[i] = (uint8_t)(value >> (8 * i));
    return append(buffer, bytes, sizeof(bytes));
}

static void swap_colors(GhosttyColorRgb *fg, int *fg_result, GhosttyColorRgb *bg, int *bg_result) {
    GhosttyColorRgb color = *fg; *fg = *bg; *bg = color;
    int result = *fg_result; *fg_result = *bg_result; *bg_result = result;
}
static int refresh_color_overrides(Terminal *state) {
    if (!state->colors_dirty) return 0;
    const GhosttyTerminalOption options[] = {
        GHOSTTY_TERMINAL_OPT_COLOR_FOREGROUND, GHOSTTY_TERMINAL_OPT_COLOR_BACKGROUND,
        GHOSTTY_TERMINAL_OPT_COLOR_CURSOR
    };
    const GhosttyTerminalData effective[] = {
        GHOSTTY_TERMINAL_DATA_COLOR_FOREGROUND, GHOSTTY_TERMINAL_DATA_COLOR_BACKGROUND,
        GHOSTTY_TERMINAL_DATA_COLOR_CURSOR
    };
    const GhosttyTerminalData defaults[] = {
        GHOSTTY_TERMINAL_DATA_COLOR_FOREGROUND_DEFAULT, GHOSTTY_TERMINAL_DATA_COLOR_BACKGROUND_DEFAULT,
        GHOSTTY_TERMINAL_DATA_COLOR_CURSOR_DEFAULT
    };
    for (size_t i = 0; i < 3; i++) {
        GhosttyColorRgb base;
        int base_result = ghostty_terminal_get(state->terminal, defaults[i], &base);
        if (base_result != GHOSTTY_SUCCESS && base_result != GHOSTTY_NO_VALUE) return base_result;
        if (base_result == GHOSTTY_SUCCESS) CHECK(ghostty_terminal_set(state->terminal, options[i], NULL));
        int result = ghostty_terminal_get(state->terminal, effective[i], &state->color_overrides[i]);
        /* Restore even on a failed query. No parser writes, callbacks, or render
         * iterators run while the default is temporarily absent. */
        int restored = base_result == GHOSTTY_SUCCESS ?
            ghostty_terminal_set(state->terminal, options[i], &base) : GHOSTTY_SUCCESS;
        CHECK(restored);
        if (result != GHOSTTY_SUCCESS && result != GHOSTTY_NO_VALUE) return result;
        state->color_results[i] = result;
    }
    state->colors_dirty = false;
    return 0;
}

/* The Python structs in vt.py define this private little-endian snapshot.
 * Pixels are fetched separately: ordinary text frames never copy images. */
int ish_vt_frame(Terminal *state, const uint8_t **out, size_t *len) {
    if (!state || !out || !len) return ISH_LIMIT;
    if (state->held) {
        *out = state->held_frame.data; *len = state->held_frame.len;
        return 0;
    }
    CHECK(refresh_color_overrides(state));
    CHECK(ghostty_render_state_update(state->render, state->terminal));
    GhosttyRenderStateCursor cursor = GHOSTTY_INIT_SIZED(GhosttyRenderStateCursor);
    GhosttyTerminalModeConfig reverse = {.mode = GHOSTTY_MODE_REVERSE_COLORS};
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_MODE, &reverse));
    /* Cell NO_VALUE means inherit the terminal default. A terminal default
     * can itself be unset (host default) or set by an OSC 10/11 override.
     * Render-state fallback RGBs discard that distinction, so do not use them. */
    GhosttyColorRgb default_fg = state->color_overrides[0], default_bg = state->color_overrides[1];
    int default_fg_result = state->color_results[0], default_bg_result = state->color_results[1];
    /* DECSCNM exchanges only terminal defaults, not explicit cell colors. */
    if (reverse.value) swap_colors(&default_fg, &default_fg_result, &default_bg, &default_bg_result);
    GhosttyTerminalScreen screen;
    GhosttyRenderStateColors palette = GHOSTTY_INIT_SIZED(GhosttyRenderStateColors);
    CHECK(ghostty_render_state_get(state->render, GHOSTTY_RENDER_STATE_DATA_COLORS, &palette));
    CHECK(ghostty_render_state_get(state->render, GHOSTTY_RENDER_STATE_DATA_CURSOR, &cursor));
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_ACTIVE_SCREEN, &screen));
    CHECK(ghostty_render_state_get(state->render, GHOSTTY_RENDER_STATE_DATA_ROW_ITERATOR, &state->rows));
    Buffer *buffer = &state->output; buffer->len = 0;
    CHECK(number(buffer, state->cols)); CHECK(number(buffer, state->height));
    CHECK(number(buffer, cursor.viewport_has_value ? cursor.viewport_x : 0)); CHECK(number(buffer, cursor.viewport_has_value ? cursor.viewport_y : 0));
    uint64_t live[8]; CHECK(ish_vt_state(state, live));
    CHECK(number(buffer, cursor.visible && cursor.viewport_has_value && live[0])); CHECK(number(buffer, screen));
    CHECK(number(buffer, live[0])); CHECK(number64(buffer, live[4])); CHECK(number64(buffer, live[5]));
    CHECK(number(buffer, cursor.visual_style)); CHECK(number(buffer, cursor.blinking));
    CHECK(number(buffer, state->cell_width)); CHECK(number(buffer, state->cell_height));
    size_t image_count_offset = buffer->len;
    CHECK(number(buffer, 0));
    uint64_t image_generation = 0;
    CHECK(ghostty_render_state_get(state->render, GHOSTTY_RENDER_STATE_DATA_IMAGE_PLACEMENTS_GENERATION, &image_generation));
    CHECK(number64(buffer, image_generation));
    CHECK(number(buffer, state->color_results[2] == GHOSTTY_SUCCESS));
    CHECK(number(buffer, state->color_overrides[2].r));
    CHECK(number(buffer, state->color_overrides[2].g));
    CHECK(number(buffer, state->color_overrides[2].b));
    uint32_t row_index = 0;
    while (ghostty_render_state_row_iterator_next(state->rows)) {
        size_t count_offset = buffer->len; uint32_t count = 0;
        uint32_t col = 0;
        CHECK(number(buffer, 0));
        CHECK(ghostty_render_state_row_get(state->rows, GHOSTTY_RENDER_STATE_ROW_DATA_CELLS, &state->cells));
        while (ghostty_render_state_row_cells_next(state->cells)) {
            GhosttyCell raw; GhosttyCellWide wide; GhosttyStyle style;
            CHECK(ghostty_render_state_row_cells_get(state->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_RAW, &raw));
            CHECK(ghostty_cell_get(raw, GHOSTTY_CELL_DATA_WIDE, &wide));
            uint32_t x = col++;
            if (wide == GHOSTTY_CELL_WIDE_SPACER_TAIL) continue;
            CHECK(ghostty_render_state_row_cells_get(state->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_STYLE, &style));
            GhosttyColorRgb fg = {0}, bg = {0};
            int fg_result = ghostty_render_state_row_cells_get(state->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_FG_COLOR, &fg);
            int bg_result = ghostty_render_state_row_cells_get(state->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_BG_COLOR, &bg);
            if (fg_result != GHOSTTY_SUCCESS && fg_result != GHOSTTY_NO_VALUE) return fg_result;
            if (bg_result != GHOSTTY_SUCCESS && bg_result != GHOSTTY_NO_VALUE) return bg_result;
            if (fg_result == GHOSTTY_NO_VALUE) { fg = default_fg; fg_result = default_fg_result; }
            if (bg_result == GHOSTTY_NO_VALUE) { bg = default_bg; bg_result = default_bg_result; }
            if (reverse.value) {
                /* PTK's unset FG/BG refer to distinct host defaults. Move each
                 * inherited channel to its host slot, then reverse the rendered
                 * pair. Explicit RGBs cancel this swap; SGR inverse composes by XOR. */
                swap_colors(&fg, &fg_result, &bg, &bg_result);
                style.flags ^= GHOSTTY_STYLE_FLAG_INVERSE;
            }
            bool selected = false;
            CHECK(ghostty_render_state_row_cells_get(state->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_SELECTED, &selected));
            if (selected) style.flags ^= GHOSTTY_STYLE_FLAG_INVERSE;
            uint8_t colors = (fg_result == GHOSTTY_SUCCESS ? ISH_COLOR_FG : 0) |
                (bg_result == GHOSTTY_SUCCESS ? ISH_COLOR_BG : 0);
            GhosttyBuffer text = {.ptr = state->grapheme.data, .cap = state->grapheme.cap};
            int result = ghostty_render_state_row_cells_get(state->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_UTF8, &text);
            if (result == GHOSTTY_OUT_OF_SPACE) {
                CHECK(reserve(&state->grapheme, text.len));
                text.ptr = state->grapheme.data; text.cap = state->grapheme.cap;
                result = ghostty_render_state_row_cells_get(state->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_UTF8, &text);
            }
            CHECK(result);
            uint32_t codepoint = 0;
            CHECK(ghostty_cell_get(raw, GHOSTTY_CELL_DATA_CODEPOINT, &codepoint));
            /* Virtual image placeholders refer to child image IDs. The outer
             * compositor emits their placements under its own namespace, so
             * forwarding their text would create a second, incorrect image. */
            if (!text.len || wide == GHOSTTY_CELL_WIDE_SPACER_HEAD || codepoint == ISH_KITTY_PLACEHOLDER) {
                text.ptr = (uint8_t *)" "; text.len = 1;
            }
            uint8_t metadata[9] = {(uint8_t)style.flags, (uint8_t)(style.flags >> 8), colors, fg.r, fg.g, fg.b, bg.r, bg.g, bg.b};
            CHECK(append(buffer, metadata, sizeof(metadata))); CHECK(number(buffer, (uint32_t)text.len));
            CHECK(number(buffer, x)); CHECK(number(buffer, wide == GHOSTTY_CELL_WIDE_WIDE ? 2 : 1));
            GhosttyColorRgb underline = {0};
            uint32_t underline_present = style.underline_color.tag != GHOSTTY_STYLE_COLOR_NONE;
            if (style.underline_color.tag == GHOSTTY_STYLE_COLOR_RGB) underline = style.underline_color.value.rgb;
            else if (style.underline_color.tag == GHOSTTY_STYLE_COLOR_PALETTE) underline = palette.palette[style.underline_color.value.palette];
            CHECK(number(buffer, underline_present));
            uint8_t underline_bytes[3] = {underline.r, underline.g, underline.b};
            CHECK(append(buffer, underline_bytes, sizeof(underline_bytes)));
            state->link.len = 0;
            bool linked = false;
            CHECK(ghostty_cell_get(raw, GHOSTTY_CELL_DATA_HAS_HYPERLINK, &linked));
            if (linked) {
                GhosttyGridRef ref;
                GhosttyPoint point = {.tag = GHOSTTY_POINT_TAG_VIEWPORT, .value = {.coordinate = {.x = x, .y = row_index}}};
                CHECK(ghostty_terminal_grid_ref(state->terminal, point, &ref));
                size_t link_len = 0;
                result = ghostty_grid_ref_hyperlink_uri(&ref, NULL, 0, &link_len);
                if (result == GHOSTTY_OUT_OF_SPACE) {
                    CHECK(reserve(&state->link, link_len));
                    CHECK(ghostty_grid_ref_hyperlink_uri(&ref, state->link.data, state->link.cap, &state->link.len));
                } else { CHECK(result); }
            }
            CHECK(number(buffer, (uint32_t)state->link.len));
            CHECK(append(buffer, text.ptr, text.len));
            CHECK(append(buffer, state->link.data, state->link.len)); count++;
        }
        put32(buffer->data + count_offset, count);
        row_index++;
    }
    CHECK(ghostty_render_state_get(state->render, GHOSTTY_RENDER_STATE_DATA_IMAGE_ITERATOR, &state->images));
    uint32_t image_count = 0;
    while (ghostty_render_state_image_iterator_next(state->images)) {
        GhosttyImageProtocol protocol;
        CHECK(ghostty_render_state_image_get(state->images, GHOSTTY_RENDER_STATE_IMAGE_DATA_PROTOCOL, &protocol));
        if (protocol != GHOSTTY_IMAGE_PROTOCOL_KITTY) continue;
        uint32_t id, placement, offset_x, offset_y, width, height, sx, sy, sw, sh;
        uint64_t generation;
        int32_t col, row, z;
        GhosttyImageLayer layer;
        const GhosttyRenderStateImageData keys[] = {
            GHOSTTY_RENDER_STATE_IMAGE_DATA_KITTY_PLACEMENT_ID, GHOSTTY_RENDER_STATE_IMAGE_DATA_IMAGE_ID,
            GHOSTTY_RENDER_STATE_IMAGE_DATA_IMAGE_GENERATION, GHOSTTY_RENDER_STATE_IMAGE_DATA_VIEWPORT_COL,
            GHOSTTY_RENDER_STATE_IMAGE_DATA_VIEWPORT_ROW, GHOSTTY_RENDER_STATE_IMAGE_DATA_CELL_OFFSET_X,
            GHOSTTY_RENDER_STATE_IMAGE_DATA_CELL_OFFSET_Y, GHOSTTY_RENDER_STATE_IMAGE_DATA_PIXEL_WIDTH,
            GHOSTTY_RENDER_STATE_IMAGE_DATA_PIXEL_HEIGHT, GHOSTTY_RENDER_STATE_IMAGE_DATA_SOURCE_X,
            GHOSTTY_RENDER_STATE_IMAGE_DATA_SOURCE_Y, GHOSTTY_RENDER_STATE_IMAGE_DATA_SOURCE_WIDTH,
            GHOSTTY_RENDER_STATE_IMAGE_DATA_SOURCE_HEIGHT, GHOSTTY_RENDER_STATE_IMAGE_DATA_LAYER,
            GHOSTTY_RENDER_STATE_IMAGE_DATA_Z
        };
        void *values[] = {&placement, &id, &generation, &col, &row, &offset_x, &offset_y,
            &width, &height, &sx, &sy, &sw, &sh, &layer, &z};
        CHECK(ghostty_render_state_image_get_multi(state->images, 15, keys, values, NULL));
        int64_t px = (int64_t)col * state->cell_width + offset_x;
        int64_t py = (int64_t)row * state->cell_height + offset_y;
        if (px < INT32_MIN || px > INT32_MAX || py < INT32_MIN || py > INT32_MAX) return ISH_LIMIT;
        CHECK(number(buffer, placement)); CHECK(number(buffer, id)); CHECK(number64(buffer, generation));
        CHECK(number(buffer, (uint32_t)px)); CHECK(number(buffer, (uint32_t)py));
        CHECK(number(buffer, width)); CHECK(number(buffer, height));
        CHECK(number(buffer, sx)); CHECK(number(buffer, sy)); CHECK(number(buffer, sw)); CHECK(number(buffer, sh));
        CHECK(number(buffer, layer)); CHECK(number(buffer, (uint32_t)z));
        image_count++;
    }
    put32(buffer->data + image_count_offset, image_count);
    CHECK(ghostty_render_state_clean(state->render));
    *out = buffer->data; *len = buffer->len;
    return 0;
}

static uint32_t read32(const uint8_t *bytes) {
    return (uint32_t)bytes[0] | ((uint32_t)bytes[1] << 8) |
        ((uint32_t)bytes[2] << 16) | ((uint32_t)bytes[3] << 24);
}
static bool held_image(Terminal *state, uint32_t id, const uint8_t **out, size_t *len) {
    for (size_t offset = 0; offset + 8 <= state->held_images.len;) {
        uint32_t image_id = read32(state->held_images.data + offset);
        uint32_t image_len = read32(state->held_images.data + offset + 4);
        if (image_id == id) {
            *out = state->held_images.data + offset + 8; *len = image_len;
            return true;
        }
        offset += 8 + image_len;
    }
    return false;
}
int ish_vt_image(Terminal *state, uint32_t id, const uint8_t **out, size_t *len) {
    if (!state || !out || !len) return ISH_LIMIT;
    *out = NULL; *len = 0;
    if (state->held) { held_image(state, id, out, len); return 0; }
    GhosttyKittyGraphics graphics;
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_KITTY_GRAPHICS, &graphics));
    if (!graphics) return 0;
    GhosttyKittyGraphicsImage image = ghostty_kitty_graphics_image(graphics, id);
    if (!image) return 0;
    uint32_t width, height;
    uint64_t generation;
    GhosttyKittyImageFormat format;
    GhosttyString pixels;
    CHECK(ghostty_kitty_graphics_image_get(image, GHOSTTY_KITTY_GRAPHICS_IMAGE_DATA_WIDTH, &width));
    CHECK(ghostty_kitty_graphics_image_get(image, GHOSTTY_KITTY_GRAPHICS_IMAGE_DATA_HEIGHT, &height));
    CHECK(ghostty_kitty_graphics_image_get(image, GHOSTTY_KITTY_GRAPHICS_IMAGE_DATA_GENERATION, &generation));
    CHECK(ghostty_kitty_graphics_image_get(image, GHOSTTY_KITTY_GRAPHICS_IMAGE_DATA_FORMAT, &format));
    int result = ghostty_kitty_graphics_image_get(image, GHOSTTY_KITTY_GRAPHICS_IMAGE_DATA_PIXELS, &pixels);
    if (result == GHOSTTY_NO_VALUE) return 0;
    CHECK(result);
    uint64_t size = (uint64_t)width * height * 4;
    if (size > ISH_MAX_BUFFER - 16) return ISH_LIMIT;
    Buffer *buffer = &state->output; buffer->len = 0;
    CHECK(number(buffer, width)); CHECK(number(buffer, height)); CHECK(number64(buffer, generation));
    if (format == GHOSTTY_KITTY_IMAGE_FORMAT_RGBA) {
        if (pixels.len != size) return ISH_LIMIT;
        CHECK(append(buffer, pixels.ptr, pixels.len));
    } else if (format == GHOSTTY_KITTY_IMAGE_FORMAT_RGB) {
        if (pixels.len != size / 4 * 3) return ISH_LIMIT;
        CHECK(reserve(buffer, size));
        for (size_t i = 0; i < pixels.len; i += 3) {
            memcpy(buffer->data + buffer->len, pixels.ptr + i, 3);
            buffer->data[buffer->len + 3] = 255; buffer->len += 4;
        }
    } else if (format == GHOSTTY_KITTY_IMAGE_FORMAT_GRAY || format == GHOSTTY_KITTY_IMAGE_FORMAT_GRAY_ALPHA) {
        size_t channels = format == GHOSTTY_KITTY_IMAGE_FORMAT_GRAY ? 1 : 2;
        if (pixels.len != size / 4 * channels) return ISH_LIMIT;
        CHECK(reserve(buffer, size));
        for (size_t i = 0; i < pixels.len; i += channels) {
            memset(buffer->data + buffer->len, pixels.ptr[i], 3);
            buffer->data[buffer->len + 3] = channels == 2 ? pixels.ptr[i + 1] : 255;
            buffer->len += 4;
        }
    } else return GHOSTTY_UNSUPPORTED;
    *out = buffer->data; *len = buffer->len;
    return 0;
}
int ish_vt_image_exists(Terminal *state, uint32_t id, bool *exists) {
    if (!state || !exists) return ISH_LIMIT;
    *exists = false;
    /* Input validation observes current storage, not the held presentation.
     * Query borrowed pixels without copying them or advancing render state. */
    GhosttyKittyGraphics graphics;
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_KITTY_GRAPHICS, &graphics));
    if (!graphics) return 0;
    GhosttyKittyGraphicsImage image = ghostty_kitty_graphics_image(graphics, id);
    if (!image) return 0;
    GhosttyString pixels;
    int result = ghostty_kitty_graphics_image_get(image, GHOSTTY_KITTY_GRAPHICS_IMAGE_DATA_PIXELS, &pixels);
    if (result == GHOSTTY_NO_VALUE) return 0;
    CHECK(result);
    *exists = true;
    return 0;
}
int ish_vt_tick(Terminal *state, uint64_t now_ms, uint64_t *next_ms) {
    if (!state || !next_ms) return ISH_LIMIT;
    *next_ms = UINT64_MAX;
    GhosttyKittyGraphics graphics;
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_KITTY_GRAPHICS, &graphics));
    if (!graphics) return 0;
    int result = ghostty_kitty_graphics_animation_tick(graphics, now_ms, next_ms);
    if (result == GHOSTTY_NO_VALUE) { *next_ms = UINT64_MAX; return 0; }
    return result;
}
static int capture_hold(Terminal *state) {
    /* The documented render_hold boundary permits querying the native state.
     * Capture presentation data, not another VT. Pixels must be copied too:
     * the child may replace/delete them before releasing the transaction. */
    const uint8_t *data; size_t len;
    release_hold(state);
    CHECK(ish_vt_frame(state, &data, &len));
    CHECK(append(&state->held_frame, data, len));
    CHECK(ghostty_render_state_get(state->render, GHOSTTY_RENDER_STATE_DATA_IMAGE_ITERATOR, &state->images));
    while (ghostty_render_state_image_iterator_next(state->images)) {
        uint32_t id;
        GhosttyImageProtocol protocol;
        CHECK(ghostty_render_state_image_get(state->images, GHOSTTY_RENDER_STATE_IMAGE_DATA_PROTOCOL, &protocol));
        if (protocol != GHOSTTY_IMAGE_PROTOCOL_KITTY) continue;
        CHECK(ghostty_render_state_image_get(state->images, GHOSTTY_RENDER_STATE_IMAGE_DATA_IMAGE_ID, &id));
        if (held_image(state, id, &data, &len)) continue;
        CHECK(ish_vt_image(state, id, &data, &len));
        if (!len) continue;
        CHECK(reserve(&state->held_images, 8 + len));
        CHECK(number(&state->held_images, id)); CHECK(number(&state->held_images, (uint32_t)len));
        CHECK(append(&state->held_images, data, len));
    }
    return 0;
}

typedef struct { const char *name; GhosttyKey key; } NamedKey;
static const NamedKey NAMED_KEYS[] = {
    {"up", GHOSTTY_KEY_ARROW_UP}, {"down", GHOSTTY_KEY_ARROW_DOWN},
    {"left", GHOSTTY_KEY_ARROW_LEFT}, {"right", GHOSTTY_KEY_ARROW_RIGHT},
    {"home", GHOSTTY_KEY_HOME}, {"end", GHOSTTY_KEY_END},
    {"pageup", GHOSTTY_KEY_PAGE_UP}, {"pagedown", GHOSTTY_KEY_PAGE_DOWN},
    {"insert", GHOSTTY_KEY_INSERT}, {"delete", GHOSTTY_KEY_DELETE},
    {"backspace", GHOSTTY_KEY_BACKSPACE}, {"enter", GHOSTTY_KEY_ENTER},
    {"tab", GHOSTTY_KEY_TAB}, {"escape", GHOSTTY_KEY_ESCAPE},
    {"f1", GHOSTTY_KEY_F1}, {"f2", GHOSTTY_KEY_F2}, {"f3", GHOSTTY_KEY_F3},
    {"f4", GHOSTTY_KEY_F4}, {"f5", GHOSTTY_KEY_F5}, {"f6", GHOSTTY_KEY_F6},
    {"f7", GHOSTTY_KEY_F7}, {"f8", GHOSTTY_KEY_F8}, {"f9", GHOSTTY_KEY_F9},
    {"f10", GHOSTTY_KEY_F10}, {"f11", GHOSTTY_KEY_F11}, {"f12", GHOSTTY_KEY_F12},
    {"f13", GHOSTTY_KEY_F13},
    {"f14", GHOSTTY_KEY_F14},
    {"f15", GHOSTTY_KEY_F15},
    {"f16", GHOSTTY_KEY_F16},
    {"f17", GHOSTTY_KEY_F17},
    {"f18", GHOSTTY_KEY_F18},
    {"f19", GHOSTTY_KEY_F19},
    {"f20", GHOSTTY_KEY_F20},
    {"f21", GHOSTTY_KEY_F21},
    {"f22", GHOSTTY_KEY_F22},
    {"f23", GHOSTTY_KEY_F23},
    {"f24", GHOSTTY_KEY_F24},
    {"f25", GHOSTTY_KEY_F25},
    {"kp0", GHOSTTY_KEY_NUMPAD_0},
    {"kp1", GHOSTTY_KEY_NUMPAD_1},
    {"kp2", GHOSTTY_KEY_NUMPAD_2},
    {"kp3", GHOSTTY_KEY_NUMPAD_3},
    {"kp4", GHOSTTY_KEY_NUMPAD_4},
    {"kp5", GHOSTTY_KEY_NUMPAD_5},
    {"kp6", GHOSTTY_KEY_NUMPAD_6},
    {"kp7", GHOSTTY_KEY_NUMPAD_7},
    {"kp8", GHOSTTY_KEY_NUMPAD_8},
    {"kp9", GHOSTTY_KEY_NUMPAD_9},
    {"kpdecimal", GHOSTTY_KEY_NUMPAD_DECIMAL},
    {"kpdivide", GHOSTTY_KEY_NUMPAD_DIVIDE},
    {"kpmultiply", GHOSTTY_KEY_NUMPAD_MULTIPLY},
    {"kpsubtract", GHOSTTY_KEY_NUMPAD_SUBTRACT},
    {"kpadd", GHOSTTY_KEY_NUMPAD_ADD},
    {"kpenter", GHOSTTY_KEY_NUMPAD_ENTER},
    {"kpequal", GHOSTTY_KEY_NUMPAD_EQUAL},
    {"kpseparator", GHOSTTY_KEY_NUMPAD_SEPARATOR},
    {"kpleft", GHOSTTY_KEY_NUMPAD_LEFT},
    {"kpright", GHOSTTY_KEY_NUMPAD_RIGHT},
    {"kpup", GHOSTTY_KEY_NUMPAD_UP},
    {"kpdown", GHOSTTY_KEY_NUMPAD_DOWN},
    {"kppageup", GHOSTTY_KEY_NUMPAD_PAGE_UP},
    {"kppagedown", GHOSTTY_KEY_NUMPAD_PAGE_DOWN},
    {"kphome", GHOSTTY_KEY_NUMPAD_HOME},
    {"kpend", GHOSTTY_KEY_NUMPAD_END},
    {"kpinsert", GHOSTTY_KEY_NUMPAD_INSERT},
    {"kpdelete", GHOSTTY_KEY_NUMPAD_DELETE},
    {"kpbegin", GHOSTTY_KEY_NUMPAD_BEGIN},
};
static GhosttyKey physical_key(uint32_t point) {
    if (point >= 'A' && point <= 'Z') point += 'a' - 'A';
    if (point >= 'a' && point <= 'z') return GHOSTTY_KEY_A + point - 'a';
    if (point >= '0' && point <= '9') return GHOSTTY_KEY_DIGIT_0 + point - '0';
    switch (point) {
        case ' ': return GHOSTTY_KEY_SPACE;
        case '[': return GHOSTTY_KEY_BRACKET_LEFT; case ']': return GHOSTTY_KEY_BRACKET_RIGHT;
        case '\\': return GHOSTTY_KEY_BACKSLASH; case '/': return GHOSTTY_KEY_SLASH;
        case '-': return GHOSTTY_KEY_MINUS; case '=': return GHOSTTY_KEY_EQUAL;
        case ',': return GHOSTTY_KEY_COMMA; case '.': return GHOSTTY_KEY_PERIOD;
        case ';': return GHOSTTY_KEY_SEMICOLON; case '\'': return GHOSTTY_KEY_QUOTE;
        case '`': return GHOSTTY_KEY_BACKQUOTE; default: return GHOSTTY_KEY_UNIDENTIFIED;
    }
}
static int keypad_separator(Terminal *state, unsigned modifiers, unsigned action) {
    if (action == GHOSTTY_KEY_ACTION_RELEASE) return 0;
    uint32_t flags = 0;
    CHECK(ish_vt_keyboard_flags(state, &flags));
    if (flags) return 0;
    GhosttyTerminalModeConfig application = {.mode = GHOSTTY_MODE_KEYPAD_KEYS};
    GhosttyTerminalModeConfig numlock = {.mode = GHOSTTY_MODE_IGNORE_KEYPAD_WITH_NUMLOCK};
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_MODE, &application));
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_MODE, &numlock));
    /* Match the native legacy keypad rule. Mode 1035 chooses numeric keys;
     * physical lock state is not inferred from a host's ambiguous text input. */
    const char *sequence = application.value && !numlock.value
        ? KEYPAD_SEPARATOR_APP[modifiers] : KEYPAD_SEPARATOR_TEXT;
    return append(&state->output, sequence, strlen(sequence));
}
int ish_vt_key(Terminal *state, const char *name, const uint8_t *text, size_t text_len,
               uint32_t point, unsigned modifiers, unsigned action, const uint8_t **out, size_t *len) {
    if (!state || !name || !out || !len || modifiers > 7 || action > 2 || text_len > ISH_MAX_BUFFER) return ISH_LIMIT;
    GhosttyKey key = physical_key(point);
    for (size_t i = 0; i < sizeof(NAMED_KEYS) / sizeof(NAMED_KEYS[0]); i++) {
        if (!strcmp(name, NAMED_KEYS[i].name)) { key = NAMED_KEYS[i].key; text = NULL; text_len = 0; point = 0; break; }
    }
    CHECK(ghostty_key_event_set_action(state->event, (GhosttyKeyAction)action));
    CHECK(ghostty_key_event_set_key(state->event, key));
    CHECK(ghostty_key_event_set_mods(state->event, modifiers));
    CHECK(ghostty_key_event_set_consumed_mods(state->event, 0));
    CHECK(ghostty_key_event_set_unshifted_codepoint(state->event, point));
    CHECK(ghostty_key_event_set_utf8(state->event, (GhosttyString){.ptr = text, .len = text_len}));
    ghostty_key_encoder_set_from_terminal(state->encoder, state->terminal);
    Buffer *buffer = &state->output; buffer->len = 0;
    CHECK(reserve(buffer, 256));
    int result = ghostty_key_encoder_encode(state->encoder, state->event, buffer->data, buffer->cap, &buffer->len);
    if (result == GHOSTTY_OUT_OF_SPACE) {
        size_t needed = buffer->len; buffer->len = 0; CHECK(reserve(buffer, needed));
        result = ghostty_key_encoder_encode(state->encoder, state->event, buffer->data, buffer->cap, &buffer->len);
    }
    CHECK(result);
    /* Prefer native encoding, including future native separator support. No
     * duplicate mode state or replacement of negotiated keyboard output. */
    if (!buffer->len && key == GHOSTTY_KEY_NUMPAD_SEPARATOR)
        CHECK(keypad_separator(state, modifiers, action));
    *out = buffer->data; *len = buffer->len;
    return 0;
}
static bool read_paste(void *userdata, GhosttyString mime, GhosttyWriter writer) {
    (void)mime;
    GhosttyString *text = userdata;
    return !text->len || writer.write(writer.userdata, text->ptr, text->len);
}
int ish_vt_paste(Terminal *state, const uint8_t *data, size_t len, const uint8_t **out, size_t *out_len) {
    if (!state || !out || !out_len || len > ISH_MAX_BUFFER || state->replies.len) return ISH_LIMIT;
    GhosttyString mime = {.ptr = (const uint8_t *)"text/plain;charset=utf-8", .len = 24};
    GhosttyString text = {.ptr = data, .len = len};
    GhosttyPaste paste = GHOSTTY_INIT_SIZED(GhosttyPaste);
    paste.source = GHOSTTY_PASTE_SOURCE_TEXT;
    paste.mimes = &mime; paste.mimes_len = 1;
    paste.reader = (GhosttyMimeReader){.read = read_paste, .userdata = &text};
    /* This is the explicit user paste action, not an automatic clipboard read. */
    paste.allow_unsafe = true;
    CHECK(ghostty_terminal_paste(state->terminal, &paste, NULL));
    return ish_vt_replies(state, out, out_len);
}

int ish_vt_mouse(Terminal *state, unsigned action, unsigned button, int32_t x, int32_t y,
                 unsigned modifiers, unsigned use_pixels, int32_t pixel_x, int32_t pixel_y,
                 const uint8_t **out, size_t *len) {
    if (!state || !out || !len || action > 2 || button > 7 || modifiers > 7) return ISH_LIMIT;
    *out = NULL; *len = 0;
    GhosttyTerminalModeConfig pixels = {.mode = GHOSTTY_MODE_MOUSE_FORMAT_SGR_PIXELS};
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_MODE, &pixels));
    /* SGR-Pixels requires a real host coordinate, never a cell centre guess. */
    if (pixels.value && !use_pixels) return 0;
    if (use_pixels && (!state->cell_width || !state->cell_height)) return ISH_LIMIT;
    CHECK(ghostty_mouse_encoder_set_from_terminal(state->mouse_encoder, state->terminal));
    GhosttyMouseEncoderSize size = GHOSTTY_INIT_SIZED(GhosttyMouseEncoderSize);
    size.cell_width = use_pixels ? state->cell_width : 1;
    size.cell_height = use_pixels ? state->cell_height : 1;
    size.screen_width = state->cols * size.cell_width; size.screen_height = state->height * size.cell_height;
    CHECK(ghostty_mouse_encoder_set(state->mouse_encoder, GHOSTTY_MOUSE_ENCODER_OPT_SIZE, &size));
    bool pressed = button >= 1 && button <= 3 && action != 1;
    CHECK(ghostty_mouse_encoder_set(state->mouse_encoder, GHOSTTY_MOUSE_ENCODER_OPT_ANY_BUTTON_PRESSED, &pressed));

    CHECK(ghostty_mouse_event_set_action(state->mouse_event, MOUSE_ACTIONS[action]));
    if (button) { CHECK(ghostty_mouse_event_set_button(state->mouse_event, MOUSE_BUTTONS[button])); }
    else { CHECK(ghostty_mouse_event_clear_button(state->mouse_event)); }
    CHECK(ghostty_mouse_event_set_mods(state->mouse_event, modifiers));
    CHECK(ghostty_mouse_event_set_position(state->mouse_event, (GhosttyMousePosition){
        .x = use_pixels ? pixel_x : x + 0.5f, .y = use_pixels ? pixel_y : y + 0.5f}));
    Buffer *buffer = &state->output; buffer->len = 0;
    CHECK(reserve(buffer, 256));
    CHECK(ghostty_mouse_encoder_encode(state->mouse_encoder, state->mouse_event, buffer->data, buffer->cap, &buffer->len));
    *out = buffer->data; *len = buffer->len;
    return 0;
}

int ish_vt_clear_selection(Terminal *state) {
    if (!state) return ISH_LIMIT;
    ghostty_selection_gesture_reset(state->gesture, state->terminal);
    ghostty_tracked_grid_ref_free(state->anchor); state->anchor = NULL;
    return ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_SELECTION, NULL);
}
static int select_output(Terminal *state, const GhosttyGridRef *ref, bool *found) {
    GhosttySelection selection = {0};
    int result = ghostty_terminal_select_output(state->terminal, ref, &selection);
    if (result == GHOSTTY_NO_VALUE) return 0;
    CHECK(result);
    /* This API copies the snapshot before any further terminal mutation. */
    CHECK(ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_SELECTION, &selection));
    *found = true;
    return 0;
}
int ish_vt_select_output(Terminal *state, unsigned x, unsigned y, bool *found) {
    if (!state || !found || x >= state->cols || y >= state->height) return ISH_LIMIT;
    *found = false;
    CHECK(ish_vt_clear_selection(state));
    GhosttyGridRef ref = {0};
    GhosttyPoint point = {.tag = GHOSTTY_POINT_TAG_VIEWPORT, .value = {.coordinate = {.x = x, .y = y}}};
    CHECK(ghostty_terminal_grid_ref(state->terminal, point, &ref));
    return select_output(state, &ref, found);
}
int ish_vt_select_last_output(Terminal *state, bool *found) {
    if (!state || !found) return ISH_LIMIT;
    *found = false;
    CHECK(ish_vt_clear_selection(state));
    if (!state->last_output) return 0;
    GhosttyTerminalScreen screen;
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_ACTIVE_SCREEN, &screen));
    if (screen != state->output_screen) return 0;
    GhosttyGridRef ref = {0};
    int result = ghostty_tracked_grid_ref_snapshot(state->last_output, &ref);
    if (result == GHOSTTY_NO_VALUE) return 0;
    CHECK(result);
    return select_output(state, &ref, found);
}
int ish_vt_selection(Terminal *state, unsigned action, int32_t x, int32_t y,
                     double position_x, double position_y, uint64_t time_ns, bool timed,
                     uint64_t interval_ns, double distance, bool rectangle, uint32_t *out) {
    if (!state || !out || action > 4 || !isfinite(position_x) || !isfinite(position_y) ||
        !isfinite(distance) || distance < 0) return ISH_LIMIT;
    if (action == 4) {
        ghostty_selection_gesture_reset(state->gesture, state->terminal);
    } else {
        GhosttySelectionGestureEvent event = NULL;
        CHECK(ghostty_selection_gesture_event_new(NULL, &event, action));
        int result = GHOSTTY_SUCCESS;
        bool inside = x >= 0 && y >= 0 && x < state->cols && y < state->height;
        GhosttyPointCoordinate coordinate = {
            .x = x < 0 ? 0 : x >= state->cols ? state->cols - 1 : x,
            .y = y < 0 ? 0 : y >= state->height ? state->height - 1 : y
        };
        GhosttyGridRef ref;
        GhosttyPoint point = {.tag = GHOSTTY_POINT_TAG_VIEWPORT, .value = {.coordinate = coordinate}};
        GhosttySurfacePosition position = {
            .x = position_x * ISH_SELECTION_UNITS, .y = position_y * ISH_SELECTION_UNITS
        };
        double repeat_distance = distance * ISH_SELECTION_UNITS;
        GhosttySelectionGestureGeometry geometry = {
            .columns = state->cols, .cell_width = ISH_SELECTION_UNITS,
            .padding_left = 0, .screen_height = state->height * ISH_SELECTION_UNITS
        };
#define EVENT_SET(key, value) do { result = ghostty_selection_gesture_event_set(event, key, value); if (result) goto event_done; } while (0)
        if (action < 3 && (action != 1 || inside)) {
            result = ghostty_terminal_grid_ref(state->terminal, point, &ref);
            if (result) goto event_done;
            EVENT_SET(GHOSTTY_SELECTION_GESTURE_EVENT_OPT_REF, &ref);
        }
        if (action != 1) EVENT_SET(GHOSTTY_SELECTION_GESTURE_EVENT_OPT_POSITION, &position);
        if (action == 0) {
            if (timed) EVENT_SET(GHOSTTY_SELECTION_GESTURE_EVENT_OPT_TIME_NS, &time_ns);
            EVENT_SET(GHOSTTY_SELECTION_GESTURE_EVENT_OPT_REPEAT_INTERVAL_NS, &interval_ns);
            EVENT_SET(GHOSTTY_SELECTION_GESTURE_EVENT_OPT_REPEAT_DISTANCE, &repeat_distance);
        }
        if (action >= 2) {
            EVENT_SET(GHOSTTY_SELECTION_GESTURE_EVENT_OPT_GEOMETRY, &geometry);
            EVENT_SET(GHOSTTY_SELECTION_GESTURE_EVENT_OPT_RECTANGLE, &rectangle);
        }
        if (action == 3) EVENT_SET(GHOSTTY_SELECTION_GESTURE_EVENT_OPT_VIEWPORT, &coordinate);
        GhosttySelection selection;
        result = ghostty_selection_gesture_event(state->gesture, state->terminal, event, &selection);
        if (result == GHOSTTY_SUCCESS) {
            result = ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_SELECTION, &selection);
        } else if (result == GHOSTTY_NO_VALUE) {
            result = action == 0 ?
                ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_SELECTION, NULL) : GHOSTTY_SUCCESS;
        }
event_done:
        ghostty_selection_gesture_event_free(event);
#undef EVENT_SET
        CHECK(result);
    }
    uint8_t count = 0; bool dragged = false;
    GhosttySelectionGestureAutoscroll autoscroll;
    GhosttySelectionGestureBehavior behavior;
    CHECK(ghostty_selection_gesture_get(state->gesture, state->terminal, GHOSTTY_SELECTION_GESTURE_DATA_CLICK_COUNT, &count));
    CHECK(ghostty_selection_gesture_get(state->gesture, state->terminal, GHOSTTY_SELECTION_GESTURE_DATA_DRAGGED, &dragged));
    CHECK(ghostty_selection_gesture_get(state->gesture, state->terminal, GHOSTTY_SELECTION_GESTURE_DATA_AUTOSCROLL, &autoscroll));
    CHECK(ghostty_selection_gesture_get(state->gesture, state->terminal, GHOSTTY_SELECTION_GESTURE_DATA_BEHAVIOR, &behavior));
    out[0] = count; out[1] = dragged; out[2] = autoscroll; out[3] = behavior;
    return 0;
}
int ish_vt_begin_selection(Terminal *state, unsigned x, unsigned y) {
    if (!state || x >= state->cols || y >= state->height) return ISH_LIMIT;
    CHECK(ish_vt_clear_selection(state));
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_ACTIVE_SCREEN, &state->anchor_screen));
    GhosttyPoint point = {.tag = GHOSTTY_POINT_TAG_VIEWPORT, .value = {.coordinate = {.x = x, .y = y}}};
    return ghostty_terminal_grid_ref_track(state->terminal, point, &state->anchor);
}
int ish_vt_update_selection(Terminal *state, unsigned x, unsigned y) {
    if (!state || x >= state->cols || y >= state->height) return ISH_LIMIT;
    if (!state->anchor) return 0;
    GhosttyTerminalScreen screen;
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_ACTIVE_SCREEN, &screen));
    if (screen != state->anchor_screen) {
        ghostty_tracked_grid_ref_free(state->anchor); state->anchor = NULL;
        return 0;
    }
    GhosttySelection selection = {0};
    int result = ghostty_tracked_grid_ref_snapshot(state->anchor, &selection.start);
    if (result == GHOSTTY_NO_VALUE) return ish_vt_clear_selection(state);
    CHECK(result);
    GhosttyPoint point = {.tag = GHOSTTY_POINT_TAG_VIEWPORT, .value = {.coordinate = {.x = x, .y = y}}};
    CHECK(ghostty_terminal_grid_ref(state->terminal, point, &selection.end));
    return ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_SELECTION, &selection);
}
static bool write_selection(void *userdata, const uint8_t *data, size_t len) {
    Buffer *buffer = userdata;
    buffer->error = append(buffer, data, len);
    return buffer->error == 0;
}
int ish_vt_selection_text(Terminal *state, const uint8_t **out, size_t *len) {
    if (!state || !out || !len) return ISH_LIMIT;
    *out = NULL; *len = 0;
    GhosttySelection selection = {0};
    int result = ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_SELECTION, &selection);
    if (result == GHOSTTY_NO_VALUE) return 0;
    CHECK(result);
    CHECK(ghostty_formatter_set(state->formatter, GHOSTTY_FORMATTER_OPT_SELECTION, &selection));
    Buffer *buffer = &state->output; buffer->len = 0; buffer->error = 0;
    result = ghostty_formatter_format(state->formatter, (GhosttyWriter){.write = write_selection, .userdata = buffer});
    /* Never keep formatter-owned snapshots across a terminal mutation. */
    int cleared = ghostty_formatter_set(state->formatter, GHOSTTY_FORMATTER_OPT_SELECTION, NULL);
    CHECK(result); CHECK(cleared); CHECK(buffer->error);
    *out = buffer->data; *len = buffer->len;
    return 0;
}

/* Classify an extension namespace without feeding or mutating a live terminal.
 * Known commands remain native-owned even when a supplied body is malformed. */
int ish_vt_osc_unknown(const unsigned char *body, size_t len) {
    if (!body || !len || len > ISH_MAX_BUFFER) return ISH_LIMIT;
    GhosttyOscParser parser = NULL;
    int status = ghostty_osc_new(NULL, &parser);
    if (status != GHOSTTY_SUCCESS) return ISH_MEMORY;
    size_t limit = len;
    status = ghostty_osc_set(parser, GHOSTTY_OSC_OPT_UNKNOWN_MAX_BYTES, &limit);
    if (status != GHOSTTY_SUCCESS) {
        ghostty_osc_free(parser);
        return ISH_LIMIT;
    }
    for (size_t i = 0; i < len; ++i) ghostty_osc_next(parser, body[i]);
    GhosttyOscCommand command = ghostty_osc_end(parser, 0x07);
    bool unknown = ghostty_osc_command_type(command) == GHOSTTY_OSC_COMMAND_UNKNOWN;
    ghostty_osc_free(parser);
    return unknown ? 1 : 0;
}
