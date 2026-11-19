#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#endif

#include "snes.h"
#include "ppu.h"
#include "interp816.h"
#include "interp_bridge.h"
#include "snes_text_xlate.h"

extern Snes *g_snes;

enum {
    VWF_VRAM_BASE_WORD   = 0x5C00u, /* VRAM byte 0xB800..0xBDFF = tiles 0x180..0x1DF (free in BG3) */
    VWF_SLOT_TILE_BASE   = 0x80u,   /* low tile byte offset: tile = 0x100 + 0x80 + slot */
    VWF_MAX_COLS         = 24u,     /* 24 * 8 = 192px */
    VWF_BANDS            = 4u,      /* 4 text rows * 24 tiles = 96 dynamic tiles */
    VWF_TILES_PER_BAND   = 24u,
    VWF_TOTAL_TILES      = 96u,

    /* Saved in unused $7F:FFF0..$7F:FFF4 so savestates/rewind preserve VWF state */
    VWF_WRAM_STATE_OFF   = 0x1FFF0u,
    VWF_MAGIC_0          = 0x56u,   /* 'V' */
    VWF_MAGIC_1          = 0x57u,   /* 'W' */

    VWF_FLAG_SKIP_B1A5   = 0x01u,
    VWF_FLAG_PREV_SPACE  = 0x02u,
};

typedef struct VwfGlyph {
    uint8_t mapped;
    uint8_t width_px;
    uint8_t advance_px;
    uint8_t code;
    uint8_t rows[8];
} VwfGlyph;

static VwfGlyph s_glyphs[256];
static int s_font_load_state = 0; /* 0 = unloaded, 1 = loaded, -1 = missing */

static void vwf_load_font(void) {
    if (s_font_load_state != 0)
        return;

    const char *candidates[2] = { "translations/vwf_font.bin", NULL };
    char exe_path_buf[1024];
#ifdef _WIN32
    DWORD len = GetModuleFileNameA(NULL, exe_path_buf, sizeof(exe_path_buf) - 64);
    if (len > 0 && len < sizeof(exe_path_buf) - 64) {
        char *slash = strrchr(exe_path_buf, '\\');
        char *fslash = strrchr(exe_path_buf, '/');
        if (fslash && (!slash || fslash > slash))
            slash = fslash;
        if (slash) {
            strcpy(slash + 1, "translations\\vwf_font.bin");
            candidates[1] = exe_path_buf;
        }
    }
#endif

    FILE *fp = NULL;
    for (int i = 0; i < 2 && !fp; i++) {
        if (candidates[i])
            fp = fopen(candidates[i], "rb");
    }
    if (!fp) {
        s_font_load_state = -1;
        return;
    }

    uint8_t hdr[16];
    if (fread(hdr, 1, sizeof(hdr), fp) != sizeof(hdr) ||
        memcmp(hdr, "SMVWF01\0", 8) != 0) {
        fclose(fp);
        s_font_load_state = -1;
        return;
    }

    uint8_t raw[256 * 12];
    if (fread(raw, 1, sizeof(raw), fp) != sizeof(raw)) {
        fclose(fp);
        s_font_load_state = -1;
        return;
    }
    fclose(fp);

    for (int i = 0; i < 256; i++) {
        const uint8_t *p = &raw[i * 12];
        s_glyphs[i].mapped = p[0];
        s_glyphs[i].width_px = p[1] ? p[1] : 8;
        s_glyphs[i].advance_px = p[2] ? p[2] : 8;
        s_glyphs[i].code = p[3];
        memcpy(s_glyphs[i].rows, p + 4, 8);
    }
    s_font_load_state = 1;
}

static int vwf_is_active(void) {
    const char *disable = getenv("SONGMASTER_DISABLE_HOST_VWF");
    if (disable && disable[0] && disable[0] != '0')
        return 0;
    vwf_load_font();
    if (s_font_load_state != 1 || !g_snes || !g_snes->ram || !g_snes->ppu)
        return 0;
    char ident[32];
    snes_text_xlate_identity_c(ident, (int)sizeof(ident));
    return strcmp(ident, "en") == 0;
}

static void vwf_fill_words(uint16_t *dst, uint32_t count, uint16_t word_val) {
    for (uint32_t i = 0; i < count; i++)
        dst[i] = word_val;
}

static void vwf_clear_band_vram(Ppu *ppu, uint8_t band) {
    uint16_t bg_word = ppu->vram[0x5000u]; /* match BG3 blank tile 0x00 (e.g. 0x00FF solid box fill) */
    uint32_t base_word = VWF_VRAM_BASE_WORD + (uint32_t)(band & 3u) * VWF_TILES_PER_BAND * 8u;
    vwf_fill_words(&ppu->vram[base_word], VWF_TILES_PER_BAND * 8u, bg_word);
    ppu->vramWriteCount++;
}

static void vwf_reset_all(uint8_t *ram, Ppu *ppu) {
    uint16_t bg_word = ppu->vram[0x5000u];
    ram[VWF_WRAM_STATE_OFF + 0] = VWF_MAGIC_0;
    ram[VWF_WRAM_STATE_OFF + 1] = VWF_MAGIC_1;
    ram[VWF_WRAM_STATE_OFF + 2] = 0u; /* band_idx */
    ram[VWF_WRAM_STATE_OFF + 3] = 0u; /* px_x */
    ram[VWF_WRAM_STATE_OFF + 4] = 0u; /* flags */
    vwf_fill_words(&ppu->vram[VWF_VRAM_BASE_WORD], VWF_TOTAL_TILES * 8u, bg_word);
    ppu->vramWriteCount++;
}

static void vwf_ensure_state(uint8_t *ram, Ppu *ppu, uint16_t dp) {
    if (ram[VWF_WRAM_STATE_OFF + 0] == VWF_MAGIC_0 &&
        ram[VWF_WRAM_STATE_OFF + 1] == VWF_MAGIC_1) {
        return;
    }
    vwf_reset_all(ram, ppu);
    uint8_t left = ram[dp + 0x0Eu];
    uint8_t cur_x = ram[dp + 0x0Au];
    if (cur_x > left && (uint8_t)(cur_x - left) < VWF_MAX_COLS) {
        ram[VWF_WRAM_STATE_OFF + 3] = (uint8_t)((cur_x - left) * 8u);
    }
}

static void vwf_advance_band(uint8_t *ram, Ppu *ppu, uint16_t dp) {
    vwf_ensure_state(ram, ppu, dp);
    uint8_t band = (uint8_t)((ram[VWF_WRAM_STATE_OFF + 2] + 1u) & 3u);
    ram[VWF_WRAM_STATE_OFF + 2] = band;
    ram[VWF_WRAM_STATE_OFF + 3] = 0u;
    ram[VWF_WRAM_STATE_OFF + 4] &= (uint8_t)~VWF_FLAG_PREV_SPACE;
    vwf_clear_band_vram(ppu, band);
}

static int vwf_slot_has_ink(const Ppu *ppu, uint8_t slot) {
    uint16_t bg_word = ppu->vram[0x5000u];
    uint32_t base = VWF_VRAM_BASE_WORD + (uint32_t)slot * 8u;
    for (int y = 0; y < 8; y++) {
        if (ppu->vram[base + (uint32_t)y] != bg_word)
            return 1;
    }
    return 0;
}

static int vwf_is_attached_punct(uint8_t ch) {
    /* Tile IDs for: ' , . ! ? : ) ] */
    return ch == 0xFAu || ch == 0xFBu || ch == 0xA0u || ch == 0x74u ||
           ch == 0x75u || ch == 0x9Bu || ch == 0xABu || ch == 0xA5u;
}

static uint16_t vwf_glyph_width(uint8_t ch) {
    if (ch == 0x00u)
        return s_glyphs[0x00].advance_px;
    if (s_glyphs[ch].mapped)
        return s_glyphs[ch].width_px;
    return 8u;
}

static uint16_t vwf_glyph_advance(uint8_t ch) {
    if (s_glyphs[ch].mapped)
        return s_glyphs[ch].advance_px;
    return 8u;
}

static uint16_t vwf_measure_word_px(Interp816 *cpu, uint8_t first_char) {
    uint16_t total = 0u;
    uint8_t prev = first_char;
    uint32_t bank = (uint32_t)cpu->db << 16;

    /* Walk consecutive non-space, non-control bytes in current bank */
    for (uint32_t k = 0; k < 32u; k++) {
        uint32_t off16 = (uint32_t)cpu->y + k;
        if (off16 > 0xFFFFu)
            break;
        /* Only read safely from ROM or WRAM, never hardware I/O */
        uint8_t b = cpu->db;
        if (!((b <= 0x3Fu && off16 >= 0x8000u) ||
              (b >= 0x80u && b <= 0xBFu && off16 >= 0x8000u) ||
              b == 0x7Eu || b == 0x7Fu)) {
            break;
        }
        uint8_t nxt = cpu->read(cpu->mem, bank | off16);
        if (nxt == 0x00u || nxt >= 0xFDu)
            break;
        if (nxt == 0x6Eu || nxt == 0x6Fu)
            continue;
        total += vwf_glyph_advance(prev);
        prev = nxt;
    }
    total += vwf_glyph_width(prev);
    return total;
}

static void vwf_blit_char(uint8_t *ram, Ppu *ppu, uint16_t dp, uint8_t ch, uint8_t max_cols) {
    uint8_t band = ram[VWF_WRAM_STATE_OFF + 2] & 3u;
    uint8_t px_x = ram[VWF_WRAM_STATE_OFF + 3];
    uint8_t box_left = ram[dp + 0x0Eu];
    uint8_t row_y = ram[dp + 0x0Bu];
    uint8_t attr_hi = (uint8_t)(ram[dp + 0x03u] | 0x01u);

    const uint16_t max_px = (uint16_t)max_cols * 8u;
    uint8_t adv = (uint8_t)vwf_glyph_advance(ch);
    uint8_t w_px = (uint8_t)vwf_glyph_width(ch);

    if (ch != 0x00u && px_x < max_px) {
        uint8_t col0 = (uint8_t)(px_x >> 3);
        uint8_t bit_shift = (uint8_t)(px_x & 7u);
        uint8_t slot0 = (uint8_t)(band * VWF_TILES_PER_BAND + col0);

        if (col0 < max_cols) {
            uint32_t vram0 = VWF_VRAM_BASE_WORD + (uint32_t)slot0 * 8u;
            uint8_t need_col1 = (bit_shift > 0u && (uint16_t)(px_x + w_px) > (uint16_t)((col0 + 1u) * 8u) && (uint8_t)(col0 + 1u) < max_cols) ? 1u : 0u;
            uint8_t slot1 = (uint8_t)(slot0 + 1u);
            uint32_t vram1 = VWF_VRAM_BASE_WORD + (uint32_t)slot1 * 8u;

            if (s_glyphs[ch].mapped) {
                const uint8_t *rows = s_glyphs[ch].rows;
                for (int y = 0; y < 8; y++) {
                    uint8_t b = rows[y];
                    uint8_t left_bits = (uint8_t)(b >> bit_shift);
                    if (left_bits)
                        ppu->vram[vram0 + (uint32_t)y] |= (uint16_t)left_bits * 0x0101u;
                    if (need_col1 && bit_shift > 0u) {
                        uint8_t right_bits = (uint8_t)((b << (8u - bit_shift)) & 0xFFu);
                        if (right_bits)
                            ppu->vram[vram1 + (uint32_t)y] |= (uint16_t)right_bits * 0x0101u;
                    }
                }
            } else {
                /* Fallback for unmapped symbols: copy glyph pixels from 0xA000 */
                uint32_t src_word = 0x5000u + (uint32_t)ch * 8u;
                uint16_t bg_word = ppu->vram[0x5000u];
                for (int y = 0; y < 8; y++) {
                    uint16_t sw = ppu->vram[src_word + (uint32_t)y];
                    uint8_t p0 = (uint8_t)((sw & 0xFFu) ^ (bg_word & 0xFFu));
                    uint8_t p1 = (uint8_t)(((sw >> 8) & 0xFFu) ^ ((bg_word >> 8) & 0xFFu));
                    ppu->vram[vram0 + (uint32_t)y] |= (uint16_t)(p0 >> bit_shift) | ((uint16_t)(p1 >> bit_shift) << 8);
                    if (need_col1 && bit_shift > 0u) {
                        uint8_t r0 = (uint8_t)((p0 << (8u - bit_shift)) & 0xFFu);
                        uint8_t r1 = (uint8_t)((p1 << (8u - bit_shift)) & 0xFFu);
                        ppu->vram[vram1 + (uint32_t)y] |= (uint16_t)r0 | ((uint16_t)r1 << 8);
                    }
                }
            }
            ppu->vramWriteCount++;

            uint32_t tm0 = (((uint32_t)(row_y & 0x1Fu)) << 6) | (((uint32_t)((box_left + col0) & 0x1Fu)) << 1);
            ram[0x4680u + tm0] = (uint8_t)(VWF_SLOT_TILE_BASE + slot0);
            /* Preserve an earlier highlight palette on a shared 8px tile when reverting to default white */
            if (bit_shift == 0u || (attr_hi & 0x1Cu) != 0x04u || ram[0x4681u + tm0] == 0x24u) {
                ram[0x4681u + tm0] = attr_hi;
            }

            if (need_col1) {
                uint32_t tm1 = (((uint32_t)(row_y & 0x1Fu)) << 6) | (((uint32_t)((box_left + col0 + 1u) & 0x1Fu)) << 1);
                ram[0x4680u + tm1] = (uint8_t)(VWF_SLOT_TILE_BASE + slot1);
                ram[0x4681u + tm1] = attr_hi;
            }
        }
    }

    uint16_t next_px = (uint16_t)px_x + (uint16_t)adv;
    if (next_px > max_px)
        next_px = max_px;
    ram[VWF_WRAM_STATE_OFF + 3] = (uint8_t)next_px;

    if (ch == 0x00u)
        ram[VWF_WRAM_STATE_OFF + 4] |= VWF_FLAG_PREV_SPACE;
    else
        ram[VWF_WRAM_STATE_OFF + 4] &= (uint8_t)~VWF_FLAG_PREV_SPACE;

    uint8_t next_col = (uint8_t)(next_px >> 3);
    if (next_col > max_cols)
        next_col = max_cols;
    ram[dp + 0x0Au] = (uint8_t)(box_left + next_col);
}

static void vwf_execute_print_and_advance_pc(Interp816 *cpu, uint8_t *ram, Ppu *ppu, uint16_t dp, uint8_t ch) {
    uint8_t flags = ram[VWF_WRAM_STATE_OFF + 4];
    if (ch == 0x00u && (flags & VWF_FLAG_SKIP_B1A5)) {
        ram[VWF_WRAM_STATE_OFF + 4] = (uint8_t)((flags & (uint8_t)~VWF_FLAG_SKIP_B1A5) | VWF_FLAG_PREV_SPACE);
        cpu->pc = 0xB1AFu; /* PLA; CMP #$00 -> BEQ $B1BB -> skip INC $0A -> PLP; RTS */
        return;
    }
    ram[VWF_WRAM_STATE_OFF + 4] = (uint8_t)(flags & (uint8_t)~VWF_FLAG_SKIP_B1A5);

    if (ch == 0x6Eu || ch == 0x6Fu) {
        if ((uint32_t)cpu->y < 0xFFFFu)
            cpu->y = (uint16_t)(cpu->y + 1u);
        cpu->pc = 0xB1AFu;
        return;
    }

    uint8_t left = ram[dp + 0x0Eu];
    uint8_t right = ram[dp + 0x0Fu];
    uint8_t max_cols = (right > left) ? (uint8_t)(right - left) : (uint8_t)VWF_MAX_COLS;
    if (max_cols > VWF_MAX_COLS) max_cols = VWF_MAX_COLS;

    vwf_blit_char(ram, ppu, dp, ch, max_cols);
    cpu->pc = 0xB1A8u; /* run native $BD96 VBlank + $B204 typewriter delay */
}

static int vwf_interp_override_hook(Interp816 *cpu, uint32_t pc24) {
    uint32_t npc = pc24 & 0x7FFFFFu;
    if ((npc >> 16) != 0x02u)
        return 0;

    /* Fast address check before calling identity/font checks */
    uint16_t addr16 = (uint16_t)(npc & 0xFFFFu);
    if (addr16 != 0xAEBEu && addr16 != 0xAFC6u && addr16 != 0xAFE1u &&
        addr16 != 0xAFBCu && addr16 != 0xB196u && addr16 != 0xB1A5u &&
        addr16 != 0xB1BBu) {
        return 0;
    }

    if (!vwf_is_active())
        return 0;

    uint8_t *ram = g_snes->ram;
    Ppu *ppu = g_snes->ppu;
    uint16_t dp = cpu->dp;

    switch (addr16) {
        case 0xAEBEu:
            /* Dialog open ($02:AEAB): if $06 == 0, fresh box is opening */
            if (ram[dp + 0x06u] == 0u)
                vwf_reset_all(ram, ppu);
            return 0;

        case 0xAFC6u:
            /* <FD:0C> Clear box */
            if (ram[dp + 0x05u] == 0u)
                vwf_reset_all(ram, ppu);
            return 0;

        case 0xAFE1u:
            /* <FD:0D> Newline (also jumped to from auto-wrap at $02:B1A0) */
            if (ram[dp + 0x05u] == 0u)
                vwf_advance_band(ram, ppu, dp);
            return 0;

        case 0xAFBCu:
            /* <FD:0B> Tab +5 tiles (+40px) */
            if (ram[dp + 0x05u] == 0u) {
                vwf_ensure_state(ram, ppu, dp);
                uint8_t left = ram[dp + 0x0Eu];
                uint8_t right = ram[dp + 0x0Fu];
                uint8_t max_cols = (right > left) ? (uint8_t)(right - left) : (uint8_t)VWF_MAX_COLS;
                if (max_cols > VWF_MAX_COLS) max_cols = VWF_MAX_COLS;
                uint16_t max_px = (uint16_t)max_cols * 8u;
                uint16_t nx = (uint16_t)ram[VWF_WRAM_STATE_OFF + 3] + 40u;
                if (nx > max_px) nx = max_px;
                ram[VWF_WRAM_STATE_OFF + 3] = (uint8_t)nx;
                ram[VWF_WRAM_STATE_OFF + 4] &= (uint8_t)~VWF_FLAG_PREV_SPACE;
            }
            return 0;

        case 0xB196u: {
            if (ram[dp + 0x05u] != 0u)
                return 0;
            vwf_ensure_state(ram, ppu, dp);
            uint8_t ch = (uint8_t)(cpu->a & 0xFFu);
            uint8_t left = ram[dp + 0x0Eu];
            uint8_t right = ram[dp + 0x0Fu];
            uint8_t max_cols = (right > left) ? (uint8_t)(right - left) : (uint8_t)VWF_MAX_COLS;
            if (max_cols > VWF_MAX_COLS) max_cols = VWF_MAX_COLS;
            uint16_t max_px = (uint16_t)max_cols * 8u;
            uint8_t px_x = ram[VWF_WRAM_STATE_OFF + 3];
            uint8_t flags = ram[VWF_WRAM_STATE_OFF + 4];

            if (ch == 0x00u) {
                uint16_t adv = vwf_glyph_advance(0x00u);
                if (px_x + adv > max_px) {
                    ram[VWF_WRAM_STATE_OFF + 4] = (uint8_t)(flags | VWF_FLAG_SKIP_B1A5);
                    cpu->pc = 0xB1A0u; /* JSR $AFE1 -> $B1A3 -> $B1A5 */
                    return 1;
                }
            } else if (ch != 0x6Eu && ch != 0x6Fu) {
                /* If a new word (or non-attached glyph) changes palette mid-tile on an inked cell,
                   snap to the next 8px tile boundary so SNES BG3 per-tile palettes never bleed. */
                if ((px_x & 7u) != 0u && ((flags & VWF_FLAG_PREV_SPACE) || !vwf_is_attached_punct(ch))) {
                    uint8_t col0 = (uint8_t)(px_x >> 3);
                    uint8_t band = ram[VWF_WRAM_STATE_OFF + 2] & 3u;
                    uint8_t slot0 = (uint8_t)(band * VWF_TILES_PER_BAND + col0);
                    uint8_t box_left = left;
                    uint8_t row_y = ram[dp + 0x0Bu];
                    uint32_t tm0 = (((uint32_t)(row_y & 0x1Fu)) << 6) | (((uint32_t)((box_left + col0) & 0x1Fu)) << 1);
                    uint8_t want_attr = (uint8_t)(ram[dp + 0x03u] | 0x01u);
                    if (ram[0x4680u + tm0] >= VWF_SLOT_TILE_BASE &&
                        (ram[0x4681u + tm0] & 0x1Cu) != (want_attr & 0x1Cu) &&
                        vwf_slot_has_ink(ppu, slot0)) {
                        uint16_t snapped = (uint16_t)(col0 + 1u) * 8u;
                        px_x = (snapped > max_px) ? (uint8_t)max_px : (uint8_t)snapped;
                        ram[VWF_WRAM_STATE_OFF + 3] = px_x;
                    }
                }
                uint16_t cur_w = vwf_glyph_width(ch);
                if (px_x > 0u && ((flags & VWF_FLAG_PREV_SPACE) || (px_x + cur_w > max_px))) {
                    uint16_t word_px = vwf_measure_word_px(cpu, ch);
                    if (px_x + word_px > max_px) {
                        ram[VWF_WRAM_STATE_OFF + 4] = (uint8_t)(flags & (uint8_t)~VWF_FLAG_SKIP_B1A5);
                        cpu->pc = 0xB1A0u; /* JSR $AFE1 -> $B1A3 -> $B1A5 */
                        return 1;
                    }
                }
            }
            /* No wrap needed: blit immediately and jump straight to $B1A8 */
            vwf_execute_print_and_advance_pc(cpu, ram, ppu, dp, ch);
            return 1;
        }

        case 0xB1A5u: {
            if (ram[dp + 0x05u] != 0u)
                return 0;
            vwf_ensure_state(ram, ppu, dp);
            vwf_execute_print_and_advance_pc(cpu, ram, ppu, dp, (uint8_t)(cpu->a & 0xFFu));
            return 1;
        }

        case 0xB1BBu:
            if (ram[dp + 0x05u] == 0u) {
                cpu->pc = 0xB1BDu; /* skip INC $0A (VWF manages cursor X) */
                return 1;
            }
            return 0;
    }

    return 0;
}

void songmaster_register_vwf_hooks(void) {
    g_interp_bridge_override_hook = vwf_interp_override_hook;
}
