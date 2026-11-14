/* Narrow, bounded ABI for ish's libghostty-vt 1.0 prototype. */
#include <ghostty/vt.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define ISH_MAX_CELLS 262144u
#define ISH_MAX_BUFFER (32u * 1024u * 1024u)
#define ISH_LIMIT (-100)
#define ISH_MEMORY (-101)
#define CHECK(call) do { int status = (call); if (status != GHOSTTY_SUCCESS) return status; } while (0)

typedef struct { uint8_t *data; size_t len, cap; int error; } Buffer;
typedef struct {
    GhosttyTerminal terminal;
    GhosttyRenderState render;
    GhosttyRenderStateRowIterator rows;
    GhosttyRenderStateRowCells cells;
    GhosttyKeyEncoder encoder;
    GhosttyKeyEvent event;
    Buffer output, replies, grapheme;
    uint16_t cols, height;
} Terminal;

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
    return true;
}
static bool device_attributes(GhosttyTerminal terminal, void *userdata, GhosttyDeviceAttributes *attrs) {
    (void)terminal; (void)userdata;
    attrs->primary.conformance_level = GHOSTTY_DA_CONFORMANCE_VT220;
    attrs->primary.features[0] = GHOSTTY_DA_FEATURE_ANSI_COLOR;
    attrs->primary.num_features = 1;
    attrs->secondary.device_type = GHOSTTY_DA_DEVICE_TYPE_VT220;
    return true;
}
static int dimensions(unsigned cols, unsigned rows) {
    return cols && rows && cols <= UINT16_MAX && rows <= UINT16_MAX &&
        (uint64_t)cols * rows <= ISH_MAX_CELLS ? 0 : ISH_LIMIT;
}
void ish_vt_free(Terminal *state) {
    if (!state) return;
    ghostty_key_event_free(state->event);
    ghostty_key_encoder_free(state->encoder);
    ghostty_render_state_row_cells_free(state->cells);
    ghostty_render_state_row_iterator_free(state->rows);
    ghostty_render_state_free(state->render);
    ghostty_terminal_free(state->terminal);
    free(state->output.data); free(state->replies.data); free(state->grapheme.data);
    free(state);
}
Terminal *ish_vt_new(unsigned cols, unsigned rows) {
    if (dimensions(cols, rows)) return NULL;
    Terminal *state = calloc(1, sizeof(*state));
    if (!state) return NULL;
    state->cols = cols; state->height = rows;
    if (ghostty_terminal_new(NULL, &state->terminal, cols, rows) ||
        ghostty_render_state_new(NULL, &state->render) ||
        ghostty_render_state_row_iterator_new(NULL, &state->rows) ||
        ghostty_render_state_row_cells_new(NULL, &state->cells) ||
        ghostty_key_encoder_new(NULL, &state->encoder) ||
        ghostty_key_event_new(NULL, &state->event) ||
        ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_USERDATA, state) ||
        ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_WRITE_PTY, (const void *)write_pty) ||
        ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_SIZE, (const void *)report_size) ||
        ghostty_terminal_set(state->terminal, GHOSTTY_TERMINAL_OPT_DEVICE_ATTRIBUTES, (const void *)device_attributes)) {
        ish_vt_free(state); return NULL;
    }
    return state;
}
int ish_vt_feed(Terminal *state, const uint8_t *data, size_t len) {
    if (!state || (!data && len)) return ISH_LIMIT;
    ghostty_terminal_vt_write(state->terminal, data, len);
    return state->replies.error;
}
int ish_vt_resize(Terminal *state, unsigned cols, unsigned rows) {
    if (!state) return ISH_LIMIT;
    CHECK(dimensions(cols, rows));
    uint16_t old_cols = state->cols, old_rows = state->height;
    state->cols = cols; state->height = rows;
    int result = ghostty_terminal_resize(state->terminal, cols, rows, 0, 0);
    if (result) { state->cols = old_cols; state->height = old_rows; }
    return result;
}
int ish_vt_replies(Terminal *state, const uint8_t **out, size_t *len) {
    if (!state || !out || !len) return ISH_LIMIT;
    *out = state->replies.data; *len = state->replies.len;
    state->replies.len = 0;
    int error = state->replies.error; state->replies.error = 0;
    return error;
}

/* Little-endian bulk snapshot: six u32 header values, then for each row a
 * u32 record count. Each record has u16 flags, RGB fg, RGB bg, u32 UTF-8
 * length, and that many bytes. Wide tails have no record; spacer heads are
 * spaces. The buffer is borrowed until the next frame/key/paste call. */
int ish_vt_frame(Terminal *state, const uint8_t **out, size_t *len) {
    if (!state || !out || !len) return ISH_LIMIT;
    CHECK(ghostty_render_state_update(state->render, state->terminal));
    GhosttyRenderStateCursor cursor = GHOSTTY_INIT_SIZED(GhosttyRenderStateCursor);
    GhosttyRenderStateColors colors = GHOSTTY_INIT_SIZED(GhosttyRenderStateColors);
    GhosttyTerminalScreen screen;
    CHECK(ghostty_render_state_get(state->render, GHOSTTY_RENDER_STATE_DATA_CURSOR, &cursor));
    CHECK(ghostty_render_state_get(state->render, GHOSTTY_RENDER_STATE_DATA_COLORS, &colors));
    CHECK(ghostty_terminal_get(state->terminal, GHOSTTY_TERMINAL_DATA_ACTIVE_SCREEN, &screen));
    CHECK(ghostty_render_state_get(state->render, GHOSTTY_RENDER_STATE_DATA_ROW_ITERATOR, &state->rows));
    Buffer *buffer = &state->output; buffer->len = 0;
    CHECK(number(buffer, state->cols)); CHECK(number(buffer, state->height));
    CHECK(number(buffer, cursor.viewport_has_value ? cursor.viewport_x : 0)); CHECK(number(buffer, cursor.viewport_has_value ? cursor.viewport_y : 0));
    CHECK(number(buffer, cursor.visible && cursor.viewport_has_value)); CHECK(number(buffer, screen));
    while (ghostty_render_state_row_iterator_next(state->rows)) {
        size_t count_offset = buffer->len; uint32_t count = 0;
        CHECK(number(buffer, 0));
        CHECK(ghostty_render_state_row_get(state->rows, GHOSTTY_RENDER_STATE_ROW_DATA_CELLS, &state->cells));
        while (ghostty_render_state_row_cells_next(state->cells)) {
            GhosttyCell raw; GhosttyCellWide wide; GhosttyStyle style;
            CHECK(ghostty_render_state_row_cells_get(state->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_RAW, &raw));
            CHECK(ghostty_cell_get(raw, GHOSTTY_CELL_DATA_WIDE, &wide));
            if (wide == GHOSTTY_CELL_WIDE_SPACER_TAIL) continue;
            CHECK(ghostty_render_state_row_cells_get(state->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_STYLE, &style));
            GhosttyColorRgb fg = colors.foreground, bg = colors.background;
            int fg_result = ghostty_render_state_row_cells_get(state->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_FG_COLOR, &fg);
            int bg_result = ghostty_render_state_row_cells_get(state->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_BG_COLOR, &bg);
            if (fg_result != GHOSTTY_SUCCESS && fg_result != GHOSTTY_NO_VALUE) return fg_result;
            if (bg_result != GHOSTTY_SUCCESS && bg_result != GHOSTTY_NO_VALUE) return bg_result;
            if (fg_result == GHOSTTY_NO_VALUE) fg = colors.foreground;
            if (bg_result == GHOSTTY_NO_VALUE) bg = colors.background;
            GhosttyBuffer text = {.ptr = state->grapheme.data, .cap = state->grapheme.cap};
            int result = ghostty_render_state_row_cells_get(state->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_UTF8, &text);
            if (result == GHOSTTY_OUT_OF_SPACE) {
                CHECK(reserve(&state->grapheme, text.len));
                text.ptr = state->grapheme.data; text.cap = state->grapheme.cap;
                result = ghostty_render_state_row_cells_get(state->cells, GHOSTTY_RENDER_STATE_ROW_CELLS_DATA_GRAPHEMES_UTF8, &text);
            }
            CHECK(result);
            if (!text.len || wide == GHOSTTY_CELL_WIDE_SPACER_HEAD) { text.ptr = (uint8_t *)" "; text.len = 1; }
            uint8_t metadata[8] = {(uint8_t)style.flags, (uint8_t)(style.flags >> 8), fg.r, fg.g, fg.b, bg.r, bg.g, bg.b};
            CHECK(append(buffer, metadata, sizeof(metadata))); CHECK(number(buffer, (uint32_t)text.len));
            CHECK(append(buffer, text.ptr, text.len)); count++;
        }
        put32(buffer->data + count_offset, count);
    }
    CHECK(ghostty_render_state_clean(state->render));
    *out = buffer->data; *len = buffer->len;
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
    {"f10", GHOSTTY_KEY_F10}, {"f11", GHOSTTY_KEY_F11}, {"f12", GHOSTTY_KEY_F12}
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
int ish_vt_key(Terminal *state, const char *name, const uint8_t *text, size_t text_len,
               uint32_t point, unsigned modifiers, const uint8_t **out, size_t *len) {
    if (!state || !name || !out || !len || modifiers > 7 || text_len > ISH_MAX_BUFFER) return ISH_LIMIT;
    GhosttyKey key = physical_key(point);
    for (size_t i = 0; i < sizeof(NAMED_KEYS) / sizeof(NAMED_KEYS[0]); i++) {
        if (!strcmp(name, NAMED_KEYS[i].name)) { key = NAMED_KEYS[i].key; text = NULL; text_len = 0; point = 0; break; }
    }
    CHECK(ghostty_key_event_set_action(state->event, GHOSTTY_KEY_ACTION_PRESS));
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
    CHECK(result); *out = buffer->data; *len = buffer->len;
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
