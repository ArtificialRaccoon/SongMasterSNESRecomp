#!/usr/bin/env python3
"""
Build a standalone playable SNES ROM (.sfc) containing:
  1. All [[rom_patch]] entries from translations/songmaster.toml
     (EN translation, script/opcode crash fixes, lowercase 1bpp menu tiles,
      and reflowed dialog line breaks)
  2. Expanded 2 MB LoROM (0x7FD7 = 0x0B) with Bank $20 hosting the native 65816
     Option B VWF renderer, word-wrap lookahead, palette-conflict tile snap,
     NMI job #8 VBlank DMA uploader, and Eteran.png glyph/width tables
  3. Recomputed SNES ROM checksum & complement at 0x7FDC..0x7FDF

Default output: ROM/Song Master (English VWF).sfc (relative to repository root)
"""

import argparse
import os
import struct
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC_ROM = ROOT / "ROM" / "Song Master (Japan).sfc"
DEFAULT_OUT_ROM = ROOT / "ROM" / "Song Master (English VWF).sfc"
DEFAULT_TOML = ROOT / "translations" / "songmaster.toml"
DEFAULT_FONT_BIN = ROOT / "translations" / "vwf_font.bin"


class Asm65816:
    """Minimal deterministic two-pass 65816 assembler for Bank $20 / hooks."""

    def __init__(self, bank: int, base: int):
        self.bank = bank
        self.base = base
        self.buf = bytearray()
        self.labels: dict[str, tuple[int, int]] = {}
        self.fixups: list[tuple[int, str, str, int]] = []
        self.pass_num = 1

    @property
    def pc(self) -> int:
        return self.base + len(self.buf)

    def label(self, name: str):
        if self.pass_num == 1:
            assert name not in self.labels, f"Duplicate label: {name}"
            self.labels[name] = (self.bank, self.pc)

    def emit(self, *bs: int):
        for b in bs:
            self.buf.append(b & 0xFF)

    def emit_bytes(self, data: bytes):
        self.buf.extend(data)

    def _resolve(self, target) -> tuple[int, int]:
        if isinstance(target, str):
            return self.labels[target]
        if isinstance(target, int):
            return ((target >> 16) & 0xFF, target & 0xFFFF)
        raise TypeError(target)

    def _emit_u16(self, val: int):
        self.buf.append(val & 0xFF)
        self.buf.append((val >> 8) & 0xFF)

    def _emit_u24(self, bank: int, addr16: int):
        self._emit_u16(addr16)
        self.buf.append(bank & 0xFF)

    def op(self, opcode: int):
        self.emit(opcode)

    def op8(self, opcode: int, val: int):
        self.emit(opcode, val & 0xFF)

    def op16(self, opcode: int, target):
        self.emit(opcode)
        if self.pass_num == 1:
            self._emit_u16(0)
        else:
            _, a16 = self._resolve(target)
            self._emit_u16(a16)

    def op24(self, opcode: int, target):
        self.emit(opcode)
        if self.pass_num == 1:
            self._emit_u24(0, 0)
        else:
            if isinstance(target, str):
                b, a16 = self.labels[target]
            else:
                b, a16 = (target >> 16) & 0xFF, target & 0xFFFF
            self._emit_u24(b, a16)

    def branch(self, opcode: int, target: str):
        self.emit(opcode)
        if self.pass_num == 1:
            self.emit(0)
        else:
            _, tgt = self.labels[target]
            rel = tgt - (self.pc + 1)
            assert -128 <= rel <= 127, f"Branch out of range to {target}: {rel}"
            self.emit(rel & 0xFF)

    # Mnemonic helpers (explicit widths so M/X mistakes are impossible)
    def php(self): self.op(0x08)
    def plp(self): self.op(0x28)
    def pha(self): self.op(0x48)
    def pla(self): self.op(0x68)
    def phx(self): self.op(0xDA)
    def plx(self): self.op(0xFA)
    def phy(self): self.op(0x5A)
    def ply(self): self.op(0x7A)
    def phb(self): self.op(0x8B)
    def plb(self): self.op(0xAB)
    def clc(self): self.op(0x18)
    def sec(self): self.op(0x38)
    def xba(self): self.op(0xEB)
    def tax(self): self.op(0xAA)
    def txa(self): self.op(0x8A)
    def tay(self): self.op(0xA8)
    def tya(self): self.op(0x98)
    def inx(self): self.op(0xE8)
    def iny(self): self.op(0xC8)
    def dex(self): self.op(0xCA)
    def dey(self): self.op(0x88)
    def inc_a(self): self.op(0x1A)
    def dec_a(self): self.op(0x3A)
    def asl_a(self): self.op(0x0A)
    def lsr_a(self): self.op(0x4A)
    def nop(self): self.op(0xEA)
    def rts(self): self.op(0x60)
    def rtl(self): self.op(0x6B)
    def sep(self, m: int): self.op8(0xE2, m)
    def rep(self, m: int): self.op8(0xC2, m)

    def lda_i8(self, v: int): self.op8(0xA9, v)
    def lda_i16(self, v): self.op16(0xA9, v)
    def lda_dp(self, dp: int): self.op8(0xA5, dp)
    def lda_abs(self, a16: int): self.op16(0xAD, a16)
    def lda_y(self, a16: int): self.op16(0xB9, a16)
    def lda_long(self, a24): self.op24(0xAF, a24)
    def lda_long_x(self, a24): self.op24(0xBF, a24)

    def ldx_i16(self, v): self.op16(0xA2, v)
    def ldy_i16(self, v): self.op16(0xA0, v)

    def sta_dp(self, dp: int): self.op8(0x85, dp)
    def sta_abs(self, a16: int): self.op16(0x8D, a16)
    def sta_long(self, a24): self.op24(0x8F, a24)
    def sta_long_x(self, a24): self.op24(0x9F, a24)
    def stx_abs(self, a16: int): self.op16(0x8E, a16)
    def stz_abs(self, a16: int): self.op16(0x9C, a16)

    def inc_dp(self, dp: int): self.op8(0xE6, dp)
    def and_i8(self, v: int): self.op8(0x29, v)
    def and_i16(self, v: int): self.op16(0x29, v)
    def ora_i8(self, v: int): self.op8(0x09, v)
    def ora_long_x(self, a24): self.op24(0x1F, a24)

    def adc_i8(self, v: int): self.op8(0x69, v)
    def adc_i16(self, v: int): self.op16(0x69, v)
    def adc_dp(self, dp: int): self.op8(0x65, dp)
    def adc_long(self, a24): self.op24(0x6F, a24)
    def adc_long_x(self, a24): self.op24(0x7F, a24)
    def sbc_dp(self, dp: int): self.op8(0xE5, dp)
    def sbc_long(self, a24): self.op24(0xEF, a24)

    def cmp_i8(self, v: int): self.op8(0xC9, v)
    def cmp_i16(self, v: int): self.op16(0xC9, v)
    def cmp_dp(self, dp: int): self.op8(0xC5, dp)
    def cmp_long(self, a24): self.op24(0xCF, a24)
    def cmp_long_x(self, a24): self.op24(0xDF, a24)
    def cpx_i16(self, v: int): self.op16(0xE0, v)
    def cpy_i16(self, v: int): self.op16(0xC0, v)

    def bcc(self, t: str): self.branch(0x90, t)
    def bcs(self, t: str): self.branch(0xB0, t)
    def beq(self, t: str): self.branch(0xF0, t)
    def bne(self, t: str): self.branch(0xD0, t)
    def bmi(self, t: str): self.branch(0x30, t)
    def bpl(self, t: str): self.branch(0x10, t)
    def bra(self, t: str): self.branch(0x80, t)

    def jsr(self, t: str): self.op16(0x20, t)
    def jmp(self, t: str): self.op16(0x4C, t)
    def jsl(self, t): self.op24(0x22, t)
    def jml(self, t): self.op24(0x5C, t)
    def jeq(self, t: str): self.emit(0xD0, 0x03); self.jmp(t)
    def jne(self, t: str): self.emit(0xF0, 0x03); self.jmp(t)
    def jcc(self, t: str): self.emit(0xB0, 0x03); self.jmp(t)
    def jcs(self, t: str): self.emit(0x90, 0x03); self.jmp(t)


def load_vwf_font_tables(font_bin_path: Path):
    blob = font_bin_path.read_bytes()
    assert blob[:8] == b"SMVWF01\x00" and len(blob) == 16 + 256 * 12
    rows_tbl = bytearray(256 * 8)
    mapped_tbl = bytearray(256)
    width_tbl = bytearray(256)
    adv_tbl = bytearray(256)
    attached_tbl = bytearray(256)

    for b in range(256):
        mapped, w_px, adv_px, _code = struct.unpack_from("<BBBB", blob, 16 + b * 12)
        rows = blob[16 + b * 12 + 4 : 16 + b * 12 + 12]
        mapped_tbl[b] = mapped
        if mapped:
            width_tbl[b] = w_px if w_px else 8
            adv_tbl[b] = adv_px if adv_px else 8
            rows_tbl[b * 8 : b * 8 + 8] = rows
        else:
            width_tbl[b] = 8
            adv_tbl[b] = 8

    for b in (0xFA, 0xFB, 0xA0, 0x74, 0x75, 0x9B, 0xAB, 0xA5):
        attached_tbl[b] = 1
    return bytes(rows_tbl), bytes(mapped_tbl), bytes(width_tbl), bytes(adv_tbl), bytes(attached_tbl)


def assemble_bank20(font_bin_path: Path) -> tuple[bytes, dict[str, tuple[int, int]]]:
    rows_tbl, mapped_tbl, width_tbl, adv_tbl, attached_tbl = load_vwf_font_tables(font_bin_path)

    SHADOW_BASE    = 0x7FE000
    ST_MAGIC0      = 0x7FE600
    ST_MAGIC1      = 0x7FE601
    ST_BAND        = 0x7FE602
    ST_PXX         = 0x7FE603
    ST_FLAGS       = 0x7FE604
    ST_DIRTY_START = 0x7FE605
    ST_DIRTY_CNT   = 0x7FE606
    ST_BG_INIT     = 0x7FE607
    ST_BG_WORD     = 0x7FE608
    TMP_CH         = 0x7FE60A
    TMP_COL0       = 0x7FE60B
    TMP_SHIFT      = 0x7FE60C
    TMP_SLOT0      = 0x7FE60D
    TMP_NEED1      = 0x7FE60E
    TMP_MAXPX      = 0x7FE60F
    TMP_MAXCOLS    = 0x7FE610
    TMP_ATTR       = 0x7FE611
    TMP_TM0        = 0x7FE612
    TMP_TM1        = 0x7FE614
    TMP_WP         = 0x7FE616
    TMP_PREV       = 0x7FE618
    TMP_K          = 0x7FE619
    TMP_DS         = 0x7FE61A
    TMP_DC         = 0x7FE61B
    TMP_GLYPH_PTR  = 0x7FE61E
    TMP_SH0        = 0x7FE620
    TMP_SH1        = 0x7FE622
    TMP_LEFT_BITS  = 0x7FE624
    TMP_RIGHT_BITS = 0x7FE625
    TMP_MAPPED     = 0x7FE626
    TMP_Y_LOOP     = 0x7FE627

    VWF_SLOT_TILE_BASE = 0x80
    VWF_VRAM_TILE_BASE = 0x5C00
    ROM_FALLBACK_FONT  = 0x108000

    a = Asm65816(0x20, 0x8000)

    def emit_pass():
        a.buf.clear()

        a.label("TBL_ROWS");     a.emit_bytes(rows_tbl)
        a.label("TBL_MAPPED");   a.emit_bytes(mapped_tbl)
        a.label("TBL_WIDTH");    a.emit_bytes(width_tbl)
        a.label("TBL_ADV");      a.emit_bytes(adv_tbl)
        a.label("TBL_ATTACHED"); a.emit_bytes(attached_tbl)

        # ---- fn_mark_dirty (M=8) ----
        a.label("fn_mark_dirty")
        a.sep(0x20)
        a.lda_long(ST_DIRTY_CNT)
        a.bne("md_merge")
        a.lda_long(TMP_DS)
        a.sta_long(ST_DIRTY_START)
        a.lda_long(TMP_DC)
        a.sta_long(ST_DIRTY_CNT)
        a.rts()
        a.label("md_merge")
        a.clc()
        a.adc_long(ST_DIRTY_START)
        a.sta_long(TMP_K)
        a.lda_long(TMP_DS)
        a.clc()
        a.adc_long(TMP_DC)
        a.cmp_long(TMP_K)
        a.bcs("md_end_ok")
        a.lda_long(TMP_K)
        a.label("md_end_ok")
        a.sta_long(TMP_K)
        a.lda_long(TMP_DS)
        a.cmp_long(ST_DIRTY_START)
        a.bcs("md_start_ok")
        a.sta_long(ST_DIRTY_START)
        a.label("md_start_ok")
        a.lda_long(TMP_K)
        a.sec()
        a.sbc_long(ST_DIRTY_START)
        a.sta_long(ST_DIRTY_CNT)
        a.rts()

        # ---- fn_reset_all ----
        a.label("fn_reset_all")
        a.php()
        a.phx()
        a.sep(0x20)
        a.rep(0x10)
        a.lda_long(ST_BG_INIT)
        a.cmp_i8(0xA5)
        a.beq("ra_bg_ok")
        a.rep(0x20)
        a.lda_i16(0x00FF)
        a.sta_long(ST_BG_WORD)
        a.sep(0x20)
        a.label("ra_bg_ok")
        a.rep(0x20)
        a.lda_long(ST_BG_WORD)
        a.ldx_i16(0x0000)
        a.label("ra_fill")
        a.sta_long_x(SHADOW_BASE)
        a.inx(); a.inx()
        a.cpx_i16(1536)
        a.bcc("ra_fill")
        a.sep(0x20)
        a.lda_i8(0x56); a.sta_long(ST_MAGIC0)
        a.lda_i8(0x57); a.sta_long(ST_MAGIC1)
        a.lda_i8(0x00)
        a.sta_long(ST_BAND)
        a.sta_long(ST_PXX)
        a.sta_long(ST_FLAGS)
        a.sta_long(ST_DIRTY_START)
        a.lda_i8(96); a.sta_long(ST_DIRTY_CNT)
        a.plx()
        a.plp()
        a.rts()

        # ---- fn_ensure_state ----
        a.label("fn_ensure_state")
        a.php()
        a.sep(0x20)
        a.lda_long(ST_MAGIC0)
        a.cmp_i8(0x56)
        a.bne("es_init")
        a.lda_long(ST_MAGIC1)
        a.cmp_i8(0x57)
        a.bne("es_init")
        a.plp()
        a.rts()
        a.label("es_init")
        a.jsr("fn_reset_all")
        a.lda_dp(0x0A)
        a.sec()
        a.sbc_dp(0x0E)
        a.bcc("es_done")
        a.cmp_i8(24)
        a.bcs("es_done")
        a.asl_a(); a.asl_a(); a.asl_a()
        a.sta_long(ST_PXX)
        a.label("es_done")
        a.plp()
        a.rts()

        # ---- fn_compute_bounds (M=8) ----
        a.label("fn_compute_bounds")
        a.lda_dp(0x0F)
        a.sec()
        a.sbc_dp(0x0E)
        a.beq("cb_def")
        a.bcc("cb_def")
        a.cmp_i8(24)
        a.bcc("cb_ok")
        a.label("cb_def")
        a.lda_i8(24)
        a.label("cb_ok")
        a.sta_long(TMP_MAXCOLS)
        a.asl_a(); a.asl_a(); a.asl_a()
        a.sta_long(TMP_MAXPX)
        a.rts()

        # ---- fn_slot_has_ink (A=slot u8; preserves X/Y; Carry=1 if inked) ----
        a.label("fn_slot_has_ink")
        a.phx()
        a.phy()
        a.rep(0x30)
        a.and_i16(0x00FF)
        a.asl_a(); a.asl_a(); a.asl_a(); a.asl_a()
        a.tax()
        a.ldy_i16(8)
        a.label("shi_loop")
        a.lda_long_x(SHADOW_BASE)
        a.cmp_long(ST_BG_WORD)
        a.bne("shi_yes")
        a.inx(); a.inx()
        a.dey()
        a.bne("shi_loop")
        a.sep(0x20)
        a.ply()
        a.plx()
        a.clc()
        a.rts()
        a.label("shi_yes")
        a.sep(0x20)
        a.ply()
        a.plx()
        a.sec()
        a.rts()

        # ---- fn_calc_tm0: A=col0 (u8), X=(($0B&31)<<6) + ((($0E+col0)&31)<<1) ----
        a.label("fn_calc_tm0")
        a.rep(0x30)
        a.and_i16(0x00FF)
        a.sta_long(TMP_TM0)
        a.lda_dp(0x0E)
        a.and_i16(0x00FF)
        a.clc()
        a.adc_long(TMP_TM0)
        a.and_i16(0x001F)
        a.asl_a()
        a.sta_long(TMP_TM0)
        a.lda_dp(0x0B)
        a.and_i16(0x001F)
        a.asl_a(); a.asl_a(); a.asl_a(); a.asl_a(); a.asl_a(); a.asl_a()
        a.clc()
        a.adc_long(TMP_TM0)
        a.tax()
        a.sep(0x20)
        a.rts()

        # ---- fn_measure_word_px (preserves Y) ----
        a.label("fn_measure_word_px")
        a.phy()
        a.sep(0x20)
        a.rep(0x10)
        a.lda_long(TMP_CH)
        a.sta_long(TMP_PREV)
        a.rep(0x20)
        a.lda_i16(0x0000)
        a.sta_long(TMP_WP)
        a.sep(0x20)
        a.lda_i8(32)
        a.sta_long(TMP_K)
        a.label("mw_loop")
        a.lda_y(0x0000)
        a.beq("mw_done")
        a.cmp_i8(0xFD)
        a.bcs("mw_done")
        a.cmp_i8(0x6E)
        a.beq("mw_next")
        a.cmp_i8(0x6F)
        a.beq("mw_next")
        a.pha()
        a.lda_long(TMP_PREV)
        a.rep(0x30)
        a.and_i16(0x00FF)
        a.tax()
        a.sep(0x20)
        a.lda_long_x("TBL_ADV")
        a.rep(0x20)
        a.and_i16(0x00FF)
        a.clc()
        a.adc_long(TMP_WP)
        a.sta_long(TMP_WP)
        a.sep(0x20)
        a.pla()
        a.sta_long(TMP_PREV)
        a.label("mw_next")
        a.iny()
        a.beq("mw_done")
        a.lda_long(TMP_K)
        a.dec_a()
        a.sta_long(TMP_K)
        a.bne("mw_loop")
        a.label("mw_done")
        a.lda_long(TMP_PREV)
        a.rep(0x30)
        a.and_i16(0x00FF)
        a.tax()
        a.sep(0x20)
        a.lda_long_x("TBL_WIDTH")
        a.rep(0x20)
        a.and_i16(0x00FF)
        a.clc()
        a.adc_long(TMP_WP)
        a.sta_long(TMP_WP)
        a.sep(0x20)
        a.ply()
        a.rts()

        # ---- HOOK 0: vwf_on_boot (00:8014, clear VWF state in WRAM even on non-zero power-on) ----
        a.label("vwf_on_boot")
        a.php()
        a.phx()
        a.rep(0x10)
        a.sep(0x20)
        a.ldx_i16(0x0000)
        a.lda_i8(0x00)
        a.label("ob_clr")
        a.sta_long_x(ST_MAGIC0)
        a.inx()
        a.cpx_i16(0x0028)
        a.bcc("ob_clr")
        a.plx()
        a.plp()
        a.lda_i8(0x8F)
        a.sta_abs(0x2100)
        a.rtl()

        # ---- HOOK 1: vwf_nmi_dma (00:86CC, M=8, X=16, DB=00) ----
        a.label("vwf_nmi_dma")
        a.lda_i8(0x01)
        a.sta_abs(0x420B)
        a.php()
        a.phb()
        a.phx()
        a.rep(0x10)
        a.sep(0x20)
        a.lda_i8(0x00)
        a.pha(); a.plb()
        a.lda_long(ST_MAGIC0)
        a.cmp_i8(0x56)
        a.jne("nmi_done")
        a.lda_long(ST_MAGIC1)
        a.cmp_i8(0x57)
        a.jne("nmi_done")
        a.lda_i8(0x80)
        a.sta_abs(0x2115)
        a.ldx_i16(0x5000)
        a.stx_abs(0x2116)
        a.lda_abs(0x2139)
        a.sta_long(ST_BG_WORD)
        a.lda_abs(0x213A)
        a.sta_long(ST_BG_WORD + 1)
        a.lda_i8(0xA5)
        a.sta_long(ST_BG_INIT)
        a.lda_long(ST_DIRTY_CNT)
        a.jeq("nmi_done")
        a.lda_long(ST_DIRTY_START)
        a.cmp_i8(96)
        a.bcc("nmi_start_ok")
        a.lda_i8(0x00)
        a.sta_long(ST_DIRTY_CNT)
        a.bra("nmi_done")
        a.label("nmi_start_ok")
        # max_cnt = 96 - ST_DIRTY_START
        a.lda_i8(96)
        a.sec()
        a.sbc_long(ST_DIRTY_START)
        a.cmp_long(ST_DIRTY_CNT)
        a.bcs("nmi_cnt_ok")
        a.sta_long(ST_DIRTY_CNT)
        a.label("nmi_cnt_ok")
        a.rep(0x20)
        a.lda_long(ST_DIRTY_START)
        a.and_i16(0x00FF)
        a.asl_a(); a.asl_a(); a.asl_a()
        a.clc()
        a.adc_i16(VWF_VRAM_TILE_BASE)
        a.sta_abs(0x2116)
        a.lda_long(ST_DIRTY_START)
        a.and_i16(0x00FF)
        a.asl_a(); a.asl_a(); a.asl_a(); a.asl_a()
        a.clc()
        a.adc_i16(0xE000)
        a.sta_abs(0x4302)
        a.lda_long(ST_DIRTY_CNT)
        a.and_i16(0x00FF)
        a.asl_a(); a.asl_a(); a.asl_a(); a.asl_a()
        a.sta_abs(0x4305)
        a.sep(0x20)
        a.lda_i8(0x80); a.sta_abs(0x2115)
        a.lda_i8(0x01); a.sta_abs(0x4300)
        a.lda_i8(0x18); a.sta_abs(0x4301)
        a.lda_i8(0x7F); a.sta_abs(0x4304)
        a.lda_i8(0x01); a.sta_abs(0x420B)
        a.lda_i8(0x00); a.sta_long(ST_DIRTY_CNT)
        a.label("nmi_done")
        a.plx()
        a.plb()
        a.plp()
        a.rtl()

        # ---- HOOK 2: vwf_on_open (02:AED8) ----
        a.label("vwf_on_open")
        a.jsr("fn_reset_all")
        a.sep(0x20)
        a.lda_i8(0x08)
        a.jsl(0x00BDCB)
        a.lda_i8(0x0A)
        a.jml(0x00BDCB)

        # ---- HOOK 3: vwf_clear (02:AFD8) ----
        a.label("vwf_clear")
        a.php()
        a.sep(0x20)
        a.lda_dp(0x05)
        a.bne("clr_skip")
        a.jsr("fn_reset_all")
        a.label("clr_skip")
        a.lda_dp(0x0E); a.sta_dp(0x0A)
        a.lda_dp(0x10); a.sta_dp(0x0B)
        a.plp()
        a.rtl()

        # ---- HOOK 4: vwf_tab (02:AFBC) ----
        a.label("vwf_tab")
        a.php()
        a.sep(0x20)
        a.lda_dp(0x05)
        a.bne("tab_orig")
        a.jsr("fn_ensure_state")
        a.jsr("fn_compute_bounds")
        a.lda_long(ST_PXX)
        a.clc()
        a.adc_i8(40)
        a.bcs("tab_clamp")
        a.cmp_long(TMP_MAXPX)
        a.bcc("tab_ok")
        a.label("tab_clamp")
        a.lda_long(TMP_MAXPX)
        a.label("tab_ok")
        a.sta_long(ST_PXX)
        a.lda_long(ST_FLAGS)
        a.and_i8(0xFD)
        a.sta_long(ST_FLAGS)
        a.label("tab_orig")
        a.lda_dp(0x0A)
        a.clc()
        a.adc_i8(0x05)
        a.sta_dp(0x0A)
        a.plp()
        a.rtl()

        # ---- HOOK 5: vwf_newline (02:AFE1) ----
        a.label("vwf_newline")
        a.sep(0x20)
        a.lda_dp(0x05)
        a.bne("nl_orig")
        a.phx()
        a.phy()
        a.rep(0x10)
        a.jsr("fn_ensure_state")
        a.lda_long(ST_BAND)
        a.inc_a()
        a.and_i8(0x03)
        a.sta_long(ST_BAND)
        a.lda_i8(0x00)
        a.sta_long(ST_PXX)
        a.lda_long(ST_FLAGS)
        a.and_i8(0xFD)
        a.sta_long(ST_FLAGS)
        a.lda_long(ST_BAND)
        a.asl_a(); a.asl_a(); a.asl_a()
        a.sta_long(TMP_DS)
        a.asl_a()
        a.clc()
        a.adc_long(TMP_DS)
        a.sta_long(TMP_DS)
        a.lda_i8(24)
        a.sta_long(TMP_DC)
        a.rep(0x30)
        a.lda_long(TMP_DS)
        a.and_i16(0x00FF)
        a.asl_a(); a.asl_a(); a.asl_a(); a.asl_a()
        a.tax()
        a.ldy_i16(192)
        a.lda_long(ST_BG_WORD)
        a.label("nl_fill")
        a.sta_long_x(SHADOW_BASE)
        a.inx(); a.inx()
        a.dey()
        a.bne("nl_fill")
        a.sep(0x20)
        a.jsr("fn_mark_dirty")
        a.ply()
        a.plx()
        a.label("nl_orig")
        a.sep(0x20)
        a.lda_dp(0x0E)
        a.sta_dp(0x0A)
        a.lda_dp(0x0B)
        a.inc_a()
        a.inc_a()
        a.rtl()

        # ---- HOOK 6: vwf_check_wrap (02:B196) ----
        a.label("vwf_check_wrap")
        a.sep(0x20)
        a.xba()
        a.lda_dp(0x05)
        a.beq("cw_dialog")
        a.bmi("cw_menu_nowrap")
        a.lda_dp(0x0A)
        a.cmp_dp(0x0F)
        a.rtl()
        a.label("cw_menu_nowrap")
        a.clc()
        a.rtl()

        a.label("cw_dialog")
        a.xba()
        a.php()
        a.phx()
        a.phy()
        a.rep(0x10)
        a.sep(0x20)
        a.sta_long(TMP_CH)
        a.jsr("fn_ensure_state")
        a.jsr("fn_compute_bounds")
        a.lda_long(TMP_CH)
        a.jeq("cw_space")
        a.cmp_i8(0x6E)
        a.jeq("cw_nowrap")
        a.cmp_i8(0x6F)
        a.jeq("cw_nowrap")

        a.lda_long(ST_PXX)
        a.and_i8(0x07)
        a.beq("cw_after_snap")
        a.lda_long(ST_FLAGS)
        a.and_i8(0x02)
        a.bne("cw_check_pal")
        a.lda_long(TMP_CH)
        a.rep(0x30)
        a.and_i16(0x00FF)
        a.tax()
        a.sep(0x20)
        a.lda_long_x("TBL_ATTACHED")
        a.bne("cw_after_snap")
        a.label("cw_check_pal")
        a.lda_long(ST_PXX)
        a.lsr_a(); a.lsr_a(); a.lsr_a()
        a.sta_long(TMP_COL0)
        a.jsr("fn_calc_tm0")
        a.lda_long_x(0x7E4680)
        a.cmp_i8(VWF_SLOT_TILE_BASE)
        a.bcc("cw_after_snap")
        a.lda_dp(0x03)
        a.and_i8(0x1C)
        a.sta_long(TMP_ATTR)
        a.lda_long_x(0x7E4681)
        a.and_i8(0x1C)
        a.cmp_long(TMP_ATTR)
        a.beq("cw_after_snap")
        a.lda_long(ST_BAND)
        a.asl_a(); a.asl_a(); a.asl_a()
        a.sta_long(TMP_SLOT0)
        a.asl_a()
        a.clc()
        a.adc_long(TMP_SLOT0)
        a.clc()
        a.adc_long(TMP_COL0)
        a.jsr("fn_slot_has_ink")
        a.bcc("cw_after_snap")
        a.lda_long(TMP_COL0)
        a.inc_a()
        a.asl_a(); a.asl_a(); a.asl_a()
        a.cmp_long(TMP_MAXPX)
        a.bcc("cw_snap_ok")
        a.lda_long(TMP_MAXPX)
        a.label("cw_snap_ok")
        a.sta_long(ST_PXX)

        a.label("cw_after_snap")
        a.lda_long(ST_PXX)
        a.jeq("cw_nowrap")
        a.lda_long(ST_FLAGS)
        a.and_i8(0x02)
        a.bne("cw_do_measure")
        a.lda_long(TMP_CH)
        a.rep(0x30)
        a.and_i16(0x00FF)
        a.tax()
        a.sep(0x20)
        a.lda_long_x("TBL_WIDTH")
        a.clc()
        a.adc_long(ST_PXX)
        a.bcs("cw_do_measure")
        a.cmp_long(TMP_MAXPX)
        a.jeq("cw_nowrap")
        a.jcc("cw_nowrap")
        a.label("cw_do_measure")
        a.jsr("fn_measure_word_px")
        a.rep(0x20)
        a.lda_long(ST_PXX)
        a.and_i16(0x00FF)
        a.clc()
        a.adc_long(TMP_WP)
        a.sta_long(TMP_WP)
        a.lda_long(TMP_MAXPX)
        a.and_i16(0x00FF)
        a.cmp_long(TMP_WP)
        a.sep(0x20)
        a.bcc("cw_wrap_nonspace")
        a.jmp("cw_nowrap")

        a.label("cw_space")
        a.lda_long("TBL_ADV")
        a.clc()
        a.adc_long(ST_PXX)
        a.bcs("cw_wrap_space")
        a.cmp_long(TMP_MAXPX)
        a.jeq("cw_nowrap")
        a.jcc("cw_nowrap")
        a.label("cw_wrap_space")
        a.lda_long(ST_FLAGS)
        a.ora_i8(0x01)
        a.sta_long(ST_FLAGS)
        a.ply(); a.plx(); a.plp()
        a.sec()
        a.rtl()

        a.label("cw_wrap_nonspace")
        a.lda_long(ST_FLAGS)
        a.and_i8(0xFE)
        a.sta_long(ST_FLAGS)
        a.ply(); a.plx(); a.plp()
        a.sec()
        a.rtl()

        a.label("cw_nowrap")
        a.lda_long(ST_FLAGS)
        a.and_i8(0xFE)
        a.sta_long(ST_FLAGS)
        a.ply(); a.plx(); a.plp()
        a.clc()
        a.rtl()

        # ---- HOOK 7: vwf_print (02:B1BF) ----
        a.label("vwf_print")
        a.php()
        a.rep(0x10)
        a.sep(0x20)
        a.xba()
        a.lda_dp(0x05)
        a.beq("pr_dialog")

        # Native $05 != 0 (menus / fixed-width)
        a.phx()
        a.xba()
        a.pha()
        a.rep(0x20)
        a.lda_dp(0x0B)
        a.and_i16(0x001F)
        a.asl_a(); a.asl_a(); a.asl_a(); a.asl_a(); a.asl_a(); a.asl_a()
        a.sta_long(TMP_TM0)
        a.lda_dp(0x0A)
        a.and_i16(0x001F)
        a.asl_a()
        a.clc()
        a.adc_long(TMP_TM0)
        a.tax()
        a.sep(0x20)
        a.pla()
        a.cmp_i8(0x6E)
        a.beq("pr_dak")
        a.cmp_i8(0x6F)
        a.bne("pr_native_wr")
        a.label("pr_dak")
        a.sta_long_x(0x7E4640)
        a.lda_dp(0x03)
        a.sta_long_x(0x7E4641)
        a.lda_y(0x0000)
        a.iny()
        a.label("pr_native_wr")
        a.sta_long_x(0x7E4680)
        a.lda_dp(0x03)
        a.sta_long_x(0x7E4681)
        a.inc_dp(0x0A)
        a.plx()
        a.plp()
        a.rtl()

        # VWF dialog path ($05 == 0)
        a.label("pr_dialog")
        a.xba()
        a.sta_long(TMP_CH)
        a.jsr("fn_ensure_state")
        a.jsr("fn_compute_bounds")
        a.lda_long(TMP_CH)
        a.bne("pr_chk_dak")
        a.lda_long(ST_FLAGS)
        a.and_i8(0x01)
        a.beq("pr_chk_dak")
        a.lda_long(ST_FLAGS)
        a.and_i8(0xFE)
        a.ora_i8(0x02)
        a.sta_long(ST_FLAGS)
        a.plp()
        a.rtl()

        a.label("pr_chk_dak")
        a.lda_long(ST_FLAGS)
        a.and_i8(0xFE)
        a.sta_long(ST_FLAGS)
        a.lda_long(TMP_CH)
        a.cmp_i8(0x6E)
        a.beq("pr_skip_dak")
        a.cmp_i8(0x6F)
        a.bne("pr_real")
        a.label("pr_skip_dak")
        a.iny()
        a.plp()
        a.rtl()

        a.label("pr_real")
        a.phx()
        a.phy()
        a.lda_long(TMP_CH)
        a.jeq("pr_advance_only")
        a.lda_long(ST_PXX)
        a.cmp_long(TMP_MAXPX)
        a.jcs("pr_advance_only")

        a.and_i8(0x07)
        a.sta_long(TMP_SHIFT)
        a.lda_long(ST_PXX)
        a.lsr_a(); a.lsr_a(); a.lsr_a()
        a.sta_long(TMP_COL0)
        a.cmp_long(TMP_MAXCOLS)
        a.jcs("pr_advance_only")

        a.lda_long(ST_BAND)
        a.asl_a(); a.asl_a(); a.asl_a()
        a.sta_long(TMP_SLOT0)
        a.asl_a()
        a.clc()
        a.adc_long(TMP_SLOT0)
        a.clc()
        a.adc_long(TMP_COL0)
        a.sta_long(TMP_SLOT0)

        a.lda_long(TMP_CH)
        a.rep(0x30)
        a.and_i16(0x00FF)
        a.tax()
        a.asl_a(); a.asl_a(); a.asl_a()
        a.sta_long(TMP_GLYPH_PTR)
        a.sep(0x20)
        a.lda_long_x("TBL_MAPPED")
        a.sta_long(TMP_MAPPED)
        a.lda_long_x("TBL_WIDTH")
        a.pha()
        a.lda_i8(0x00)
        a.sta_long(TMP_NEED1)
        a.lda_long(TMP_SHIFT)
        a.beq("pr_n1_pop")
        a.lda_long(TMP_COL0)
        a.inc_a()
        a.cmp_long(TMP_MAXCOLS)
        a.bcs("pr_n1_pop")
        a.asl_a(); a.asl_a(); a.asl_a()
        a.sta_long(TMP_K)
        a.pla()
        a.clc()
        a.adc_long(ST_PXX)
        a.bcs("pr_n1_yes")
        a.cmp_long(TMP_K)
        a.beq("pr_n1_done")
        a.bcc("pr_n1_done")
        a.label("pr_n1_yes")
        a.lda_i8(0x01)
        a.sta_long(TMP_NEED1)
        a.bra("pr_n1_done")
        a.label("pr_n1_pop")
        a.pla()
        a.label("pr_n1_done")

        # SH0 = slot0 * 16, SH1 = SH0 + 16
        a.rep(0x20)
        a.lda_long(TMP_SLOT0)
        a.and_i16(0x00FF)
        a.asl_a(); a.asl_a(); a.asl_a(); a.asl_a()
        a.sta_long(TMP_SH0)
        a.clc()
        a.adc_i16(16)
        a.sta_long(TMP_SH1)
        a.sep(0x20)

        # Row loop 0..7
        a.lda_i8(0x00)
        a.sta_long(TMP_Y_LOOP)
        a.label("pr_row_loop")
        a.rep(0x30)
        a.lda_long(TMP_GLYPH_PTR)
        a.tax()
        a.sep(0x20)
        a.lda_long(TMP_MAPPED)
        a.beq("pr_unmapped_row")
        a.lda_long_x("TBL_ROWS")
        a.bra("pr_have_row")
        a.label("pr_unmapped_row")
        a.lda_long_x(ROM_FALLBACK_FONT)
        a.label("pr_have_row")

        # Shift (row << 8) right by TMP_SHIFT into (left=hi, right=lo)
        a.sep(0x20)
        a.sta_long(TMP_LEFT_BITS)
        a.rep(0x30)
        a.lda_long(TMP_SHIFT)
        a.and_i16(0x00FF)
        a.tay()
        a.lda_long(TMP_LEFT_BITS)
        a.xba()
        a.and_i16(0xFF00)
        a.cpy_i16(0x0000)
        a.beq("pr_sh_done16")
        a.label("pr_sh_lp")
        a.lsr_a()
        a.dey()
        a.bne("pr_sh_lp")
        a.label("pr_sh_done16")
        # Now 16-bit A: hi byte = left_bits, lo byte = right_bits
        a.sep(0x20)
        a.sta_long(TMP_RIGHT_BITS)
        a.xba()
        a.sta_long(TMP_LEFT_BITS)

        # Blit TMP_LEFT_BITS into SH0 (both plane0 & plane1)
        a.beq("pr_skip_left")
        a.rep(0x30)
        a.lda_long(TMP_SH0)
        a.tax()
        a.sep(0x20)
        a.lda_long(TMP_LEFT_BITS)
        a.ora_long_x(SHADOW_BASE)
        a.sta_long_x(SHADOW_BASE)
        a.lda_long(TMP_LEFT_BITS)
        a.ora_long_x(SHADOW_BASE + 1)
        a.sta_long_x(SHADOW_BASE + 1)
        a.label("pr_skip_left")

        # Blit TMP_RIGHT_BITS into SH1 if TMP_NEED1 != 0
        a.lda_long(TMP_NEED1)
        a.beq("pr_skip_right")
        a.lda_long(TMP_RIGHT_BITS)
        a.beq("pr_skip_right")
        a.rep(0x30)
        a.lda_long(TMP_SH1)
        a.tax()
        a.sep(0x20)
        a.lda_long(TMP_RIGHT_BITS)
        a.ora_long_x(SHADOW_BASE)
        a.sta_long_x(SHADOW_BASE)
        a.lda_long(TMP_RIGHT_BITS)
        a.ora_long_x(SHADOW_BASE + 1)
        a.sta_long_x(SHADOW_BASE + 1)
        a.label("pr_skip_right")

        # Advance SH0, SH1, GLYPH_PTR, Y_LOOP
        a.rep(0x20)
        a.lda_long(TMP_SH0); a.inc_a(); a.inc_a(); a.sta_long(TMP_SH0)
        a.lda_long(TMP_SH1); a.inc_a(); a.inc_a(); a.sta_long(TMP_SH1)
        a.lda_long(TMP_GLYPH_PTR); a.inc_a(); a.sta_long(TMP_GLYPH_PTR)
        a.sep(0x20)
        a.lda_long(TMP_Y_LOOP)
        a.inc_a()
        a.sta_long(TMP_Y_LOOP)
        a.cmp_i8(8)
        a.jcc("pr_row_loop")

        # Mark dirty [slot0, 1 + need1)
        a.lda_long(TMP_SLOT0)
        a.sta_long(TMP_DS)
        a.lda_i8(0x01)
        a.clc()
        a.adc_long(TMP_NEED1)
        a.sta_long(TMP_DC)
        a.jsr("fn_mark_dirty")

        # Update BG3 tilemap ($7E:4680) at tm0 (and tm1 if need_col1)
        a.lda_dp(0x03)
        a.ora_i8(0x01)
        a.sta_long(TMP_ATTR)

        a.lda_long(TMP_COL0)
        a.jsr("fn_calc_tm0")    # X = tm0
        a.lda_long(TMP_SLOT0)
        a.clc()
        a.adc_i8(VWF_SLOT_TILE_BASE)
        a.sta_long_x(0x7E4680)
        # Preserve highlight palette on shared 8px tile when reverting to default white (0x04)
        a.lda_long(TMP_SHIFT)
        a.beq("pr_wr_attr0")
        a.lda_long(TMP_ATTR)
        a.and_i8(0x1C)
        a.cmp_i8(0x04)
        a.bne("pr_wr_attr0")
        a.lda_long_x(0x7E4681)
        a.cmp_i8(0x24)
        a.bne("pr_skip_attr0")
        a.label("pr_wr_attr0")
        a.lda_long(TMP_ATTR)
        a.sta_long_x(0x7E4681)
        a.label("pr_skip_attr0")

        a.lda_long(TMP_NEED1)
        a.jeq("pr_advance_only")
        a.lda_long(TMP_COL0)
        a.inc_a()
        a.jsr("fn_calc_tm0")
        a.lda_long(TMP_SLOT0)
        a.inc_a()
        a.clc()
        a.adc_i8(VWF_SLOT_TILE_BASE)
        a.sta_long_x(0x7E4680)
        a.lda_long(TMP_ATTR)
        a.sta_long_x(0x7E4681)

        # Advance px_x += adv[ch], clamp <= max_px, update PREV_SPACE & $0A
        a.label("pr_advance_only")
        a.lda_long(TMP_CH)
        a.rep(0x30)
        a.and_i16(0x00FF)
        a.tax()
        a.sep(0x20)
        a.lda_long_x("TBL_ADV")
        a.clc()
        a.adc_long(ST_PXX)
        a.bcs("pr_px_clamp")
        a.cmp_long(TMP_MAXPX)
        a.bcc("pr_px_ok")
        a.label("pr_px_clamp")
        a.lda_long(TMP_MAXPX)
        a.label("pr_px_ok")
        a.sta_long(ST_PXX)

        # PREV_SPACE = (TMP_CH == 0)
        a.lda_long(TMP_CH)
        a.beq("pr_set_ps")
        a.lda_long(ST_FLAGS)
        a.and_i8(0xFD)
        a.bra("pr_save_ps")
        a.label("pr_set_ps")
        a.lda_long(ST_FLAGS)
        a.ora_i8(0x02)
        a.label("pr_save_ps")
        a.sta_long(ST_FLAGS)

        # Sync $0A = $0E + min(max_cols, px_x >> 3)
        a.lda_long(ST_PXX)
        a.lsr_a(); a.lsr_a(); a.lsr_a()
        a.cmp_long(TMP_MAXCOLS)
        a.bcc("pr_col_ok")
        a.lda_long(TMP_MAXCOLS)
        a.label("pr_col_ok")
        a.clc()
        a.adc_dp(0x0E)
        a.sta_dp(0x0A)

        a.ply()
        a.plx()
        a.plp()
        a.rtl()

    a.pass_num = 1
    emit_pass()
    a.pass_num = 2
    emit_pass()
    return bytes(a.buf), a.labels


def pc24(labels, name: str) -> bytes:
    b, addr16 = labels[name]
    return bytes([addr16 & 0xFF, (addr16 >> 8) & 0xFF, b & 0xFF])


def patch_at(rom: bytearray, rom_off: int, expected: bytes, replacement: bytes, desc: str):
    assert len(expected) == len(replacement), f"{desc}: len mismatch {len(expected)} != {len(replacement)}"
    actual = bytes(rom[rom_off : rom_off + len(expected)])
    assert actual == expected, f"{desc} @ 0x{rom_off:06X}: expected {expected.hex()} got {actual.hex()}"
    rom[rom_off : rom_off + len(replacement)] = replacement


def main():
    ap = argparse.ArgumentParser(description="Build standalone translated + VWF patched Song Master .sfc ROM.")
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC_ROM)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_ROM)
    ap.add_argument("--toml", type=Path, default=DEFAULT_TOML)
    ap.add_argument("--font-bin", type=Path, default=DEFAULT_FONT_BIN)
    args = ap.parse_args()

    src_bytes = args.src.read_bytes()
    assert len(src_bytes) == 0x100000, f"Expected 1 MB unheadered LoROM, got {len(src_bytes)}"

    # Expand LoROM 1 MB -> 2 MB (0xFF padded)
    rom = bytearray(src_bytes) + bytearray(b"\xff" * 0x100000)

    # 1. Apply all [[rom_patch]] entries from songmaster.toml
    toml_data = tomllib.loads(args.toml.read_text(encoding="utf-8"))
    glyph_map = {g["char"]: bytes.fromhex(g["hex"]) for g in toml_data.get("glyph", [])}

    applied = 0
    for p in toml_data.get("rom_patch", []):
        addr = p["address"]
        src_hex = bytes.fromhex(p["source_hex"].replace(" ", ""))
        if "en_hex" in p:
            rep = bytes.fromhex(p["en_hex"].replace(" ", ""))
        elif "en" in p:
            rep = b"".join(glyph_map[ch] for ch in p["en"])
        else:
            continue
        assert len(src_hex) == len(rep), f"Patch @ 0x{addr:06X}: size mismatch"
        patch_at(rom, addr, src_hex, rep, f"toml_patch@0x{addr:06X}")
        applied += 1

    # 2. Assemble Bank $20 VWF engine + Eteran font tables at ROM 0x100000 ($20:8000)
    b20_bin, labels = assemble_bank20(args.font_bin)
    assert len(b20_bin) <= 0x8000, f"Bank $20 overflow: {len(b20_bin)}"
    rom[0x100000 : 0x100000 + len(b20_bin)] = b20_bin

    # 3. Install 65816 Bank 00 & Bank 02 hooks
    # 00:8014 (ROM 0x000014): LDA #$8F; STA $2100 -> JSL vwf_on_boot; NOP
    patch_at(rom, 0x000014, bytes.fromhex("a98f8d0021"), b"\x22" + pc24(labels, "vwf_on_boot") + b"\xea", "00:8014 boot clear")

    # 00:86CC (ROM 0x0006CC): LDA #$01; STA $420B -> JSL vwf_nmi_dma; NOP
    patch_at(rom, 0x0006CC, bytes.fromhex("a9018d0b42"), b"\x22" + pc24(labels, "vwf_nmi_dma") + b"\xea", "00:86CC NMI#8 DMA")

    # 02:AED8 (ROM 0x012ED8): LDA #$0A; JSL $00BDCB -> JSL vwf_on_open; NOP; NOP
    patch_at(rom, 0x012ED8, bytes.fromhex("a90a22cbbd00"), b"\x22" + pc24(labels, "vwf_on_open") + b"\xea\xea", "02:AED8 open box")

    # 02:AFBC (ROM 0x012FBC): SEP #$20; LDA $0A; CLC; ADC #$05; STA $0A; RTS -> JSL vwf_tab; RTS; NOP*5
    patch_at(rom, 0x012FBC, bytes.fromhex("e220a50a186905850a60"), b"\x22" + pc24(labels, "vwf_tab") + b"\x60\xea\xea\xea\xea\xea", "02:AFBC tab")

    # 02:AFD8 (ROM 0x012FD8): LDA $0E; STA $0A; LDA $10; STA $0B; RTS -> JSL vwf_clear; RTS; NOP*4
    patch_at(rom, 0x012FD8, bytes.fromhex("a50e850aa510850b60"), b"\x22" + pc24(labels, "vwf_clear") + b"\x60\xea\xea\xea\xea", "02:AFD8 clear")

    # 02:AFE1 (ROM 0x012FE1): SEP #$20; LDA $0E; STA $0A; LDA $0B; INC; INC -> JSL vwf_newline; NOP*6
    patch_at(rom, 0x012FE1, bytes.fromhex("e220a50e850aa50b1a1a"), b"\x22" + pc24(labels, "vwf_newline") + b"\xea\xea\xea\xea\xea\xea", "02:AFE1 newline")

    # 02:B196 (ROM 0x013196): LDA $05; BMI $B1A3; LDA $0A; CMP $0F; BCC $B1A3 -> JSL vwf_check_wrap; BCC $B1A3 (+7); NOP*4
    patch_at(rom, 0x013196, bytes.fromhex("a5053009a50ac50f9003"), b"\x22" + pc24(labels, "vwf_check_wrap") + b"\x90\x07\xea\xea\xea\xea", "02:B196 wrap check")

    # 02:B1BB (ROM 0x0131BB): INC $0A -> NOP; NOP (vwf_print manages $0A for both VWF & menus)
    patch_at(rom, 0x0131BB, bytes.fromhex("e60a"), b"\xea\xea", "02:B1BB skip INC $0A")

    # 02:B1BF (ROM 0x0131BF): JSL vwf_print; RTS (inside 40-byte B1BF body)
    orig_b1bf = bytes(rom[0x0131BF : 0x0131BF + 5])
    assert orig_b1bf == bytes.fromhex("08e22020e7"), f"Unexpected 02:B1BF prologue: {orig_b1bf.hex()}"
    rom[0x0131BF : 0x0131BF + 5] = b"\x22" + pc24(labels, "vwf_print") + b"\x60"

    # 4. Update LoROM size byte (2 MB = 0x0B) & recompute SNES checksum
    rom[0x7FD7] = 0x0B
    rom[0x7FDC : 0x7FE0] = b"\xff\xff\x00\x00"
    chk = sum(rom) & 0xFFFF
    comp = chk ^ 0xFFFF
    struct.pack_into("<HH", rom, 0x7FDC, comp, chk)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(bytes(rom))
    print(f"Applied {applied} TOML rom_patches")
    print(f"Assembled Bank $20 VWF + Eteran font: {len(b20_bin)} / 32768 bytes")
    print(f"Header size=0x{rom[0x7FD7]:02X} (2 MB), checksum=0x{chk:04X}, complement=0x{comp:04X}")
    print(f"Wrote standalone ROM: {args.out} ({len(rom)} bytes)")


if __name__ == "__main__":
    main()
