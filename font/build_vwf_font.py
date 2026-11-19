#!/usr/bin/env python3
"""
Auto-measure glyph widths from font/Eteran.png, update font/vwf_widths.json,
and compile translations/vwf_font.bin for SongMasterSNESRecomp's Option B VWF.

Grid layout of Eteran.png (128x48, 16x6 cells of 8x8 pixels):
  Codepoints 0x20 (' ') through 0x7E ('~'), plus 0x7F ('♥').
"""

import json
import os
import struct
import tomllib
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
FONT_PNG = ROOT / "font" / "Eteran.png"
WIDTHS_JSON = ROOT / "font" / "vwf_widths.json"
TOML_PATH = ROOT / "translations" / "songmaster.toml"
OUT_BIN = ROOT / "translations" / "vwf_font.bin"
RELEASE_BIN = ROOT / "build" / "Release" / "translations" / "vwf_font.bin"

MAGIC = b"SMVWF01\x00"


def measure_png(png_path: Path):
    img = Image.open(png_path).convert("RGBA")
    if img.size != (128, 48):
        raise ValueError(f"Expected 128x48 font sheet, got {img.size}")
    px = img.load()
    glyphs = {}
    for code in range(0x20, 0x80):
        idx = code - 0x20
        gx = (idx % 16) * 8
        gy = (idx // 16) * 8
        min_x, max_x = 8, -1
        rows = []
        for y in range(8):
            row_bits = 0
            for x in range(8):
                r, g, b, a = px[gx + x, gy + y]
                if a >= 128 and (r + g + b) >= 384:
                    row_bits |= (0x80 >> x)
                    if x < min_x:
                        min_x = x
                    if x > max_x:
                        max_x = x
            rows.append(row_bits)

        ch = "♥" if code == 0x7F else chr(code)
        if max_x < 0:
            # Blank cell (space): 3px advance by default
            width_px = 3 if code == 0x20 else 4
            adv_px = width_px
            min_x = 0
            ink_width = 0
        else:
            # Keep deliberate 1px left bearing (e.g. '1', '/', '\') by measuring to max_x + 1
            width_px = max_x + 1
            adv_px = min(8, width_px + 1)
            ink_width = max_x - min_x + 1

        glyphs[code] = {
            "code": code,
            "char": ch,
            "min_x": min_x,
            "max_x": max_x,
            "ink_width": ink_width,
            "width_px": width_px,
            "advance_px": adv_px,
            "rows": rows,
        }
    return glyphs


def load_existing_overrides(json_path: Path):
    overrides = {}
    if not json_path.exists():
        return overrides
    try:
        old = json.loads(json_path.read_text(encoding="utf-8"))
        # Support previous format ("widths": {"A": {"advance_px": ...}}) or flat entries
        entries = old.get("glyphs") or old.get("widths") or {}
        if isinstance(entries, dict):
            for k, v in entries.items():
                if isinstance(v, dict) and "override_advance_px" in v and v["override_advance_px"] is not None:
                    code = int(v["code"]) if "code" in v else (0x7F if k == "♥" else ord(k[0]))
                    overrides[code] = int(v["override_advance_px"])
    except Exception:
        pass
    return overrides


def main():
    glyphs = measure_png(FONT_PNG)
    overrides = load_existing_overrides(WIDTHS_JSON)

    json_entries = {}
    for code in range(0x20, 0x80):
        g = glyphs[code]
        if code in overrides:
            g["advance_px"] = max(1, min(8, overrides[code]))
            g["override_advance_px"] = overrides[code]
        key = f"0x{code:02X}"
        entry = {
            "code": code,
            "char": g["char"],
            "min_x": g["min_x"],
            "max_x": g["max_x"],
            "ink_width": g["ink_width"],
            "width_px": g["width_px"],
            "advance_px": g["advance_px"],
        }
        if code in overrides:
            entry["override_advance_px"] = overrides[code]
        json_entries[key] = entry

    doc = {
        "font": FONT_PNG.name,
        "cell_size": [8, 8],
        "sheet_grid": [16, 6],
        "measurement_rule": "width_px = max_x + 1 (keeps left bearing for tabular '1'); advance_px = min(8, width_px + 1); space = 3px. Set optional 'override_advance_px' on any glyph to customize.",
        "glyphs": json_entries,
    }
    WIDTHS_JSON.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Map Song Master byte values -> Eteran glyph codepoints from songmaster.toml
    toml_data = tomllib.loads(TOML_PATH.read_text(encoding="utf-8"))
    char_to_code = {("♥" if c == 0x7F else chr(c)): c for c in range(0x20, 0x80)}

    table = [None] * 256
    mapped_count = 0
    for item in toml_data.get("glyph", []):
        ch = item.get("char", "")
        hx = item.get("hex", "").strip()
        if not ch or len(hx) != 2 or ch not in char_to_code:
            continue
        byte_val = int(hx, 16)
        code = char_to_code[ch]
        table[byte_val] = glyphs[code]
        mapped_count += 1

    # Also map extra EN punctuation bytes used by event scripts + space (0x00)
    extra_en_map = {
        0x00: 0x20,  # ' '
        0x9A: ord('_'),
        0xA4: ord('['),
        0xA5: ord(']'),
        0xAA: ord('('),
        0xAB: ord(')'),
        0xAD: 0x7F,  # '♥'
    }
    for byte_val, code in extra_en_map.items():
        if table[byte_val] is None:
            table[byte_val] = glyphs[code]
            mapped_count += 1

    buf = bytearray()
    buf += struct.pack("<8sIIII", MAGIC, 1, 256, mapped_count, 0)[:16]
    # Header is 16 bytes: 8s (magic) + 4B (version, count_lo, mapped_count, reserved)
    buf = bytearray()
    buf += struct.pack("<8sII", MAGIC, 1, mapped_count)
    for b in range(256):
        g = table[b]
        if g is None:
            buf += bytes(12)
        else:
            buf += struct.pack("<BBBB8s", 1, g["width_px"], g["advance_px"], g["code"], bytes(g["rows"]))

    assert len(buf) == 16 + 256 * 12
    OUT_BIN.write_bytes(buf)
    if RELEASE_BIN.parent.exists():
        RELEASE_BIN.write_bytes(buf)

    print(f"Measured {len(glyphs)} glyphs from {FONT_PNG}")
    print(f"Updated {WIDTHS_JSON}")
    print(f"Wrote {OUT_BIN} ({len(buf)} bytes, {mapped_count} Song Master tile IDs mapped)")
    # Summary by advance width
    by_adv = {}
    for code in range(0x20, 0x80):
        g = glyphs[code]
        by_adv.setdefault(g["advance_px"], []).append(g["char"])
    for adv in sorted(by_adv):
        print(f"  adv={adv}px ({len(by_adv[adv]):2}): {' '.join(by_adv[adv])}")


if __name__ == "__main__":
    main()
