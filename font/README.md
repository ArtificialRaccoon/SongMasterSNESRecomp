# Song Master VWF Font Template (Option B: SNES 2bpp Dynamic Tiles)

## Files in This Folder
- `vwf_font_example.png` (`128×48` RGBA PNG, 1:1 native pixel grid):
  Edit this file (or save your own font with this exact layout) to replace the dialog font.
- `vwf_font_guide_4x.png` (`800×300` enlarged reference):
  Shows cell boundaries, **blue baseline** (`row 6`), **plum descender row** (`row 7`), and **green width edge** (`width_px`) for every glyph.
- `vwf_widths.json`:
  Character map, ROM tile hex IDs, and pixel widths (`advance_px = width_px + 1px` gap by default).

## Layout Rules (`128×48` Sheet)
- **Grid**: `16 columns × 6 rows` of **`8×8` pixel cells** (`128×48` total), ordered in standard ASCII (`0x20`–`0x7E`, with `♥` at `0x7F`):
  - Row 0 (`0x20..0x2F`): `  ! " # $ % & ' ( ) * + , - . /`
  - Row 1 (`0x30..0x3F`): `0 1 2 3 4 5 6 7 8 9 : ; < = > ?`
  - Row 2 (`0x40..0x4F`): `@ A B C D E F G H I J K L M N O`
  - Row 3 (`0x50..0x5F`): `P Q R S T U V W X Y Z [ \ ] ^ _`
  - Row 4 (`0x60..0x6F`): `` ` a b c d e f g h i j k l m n o ``
  - Row 5 (`0x70..0x7F`): `p q r s t u v w x y z { | } ~ ♥`
- **Alignment inside each `8×8` cell**:
  - **Left-align** every glyph starting at `x = 0` inside its `8×8` cell (leaving empty pixels on the right).
  - **8px tall (`rows 0..7`)**:
    - `row 0`: headroom (leave blank on normal caps/lowercase so lines breathe)
    - `rows 1–6`: caps & body (`row 6` = baseline)
    - `row 7`: descenders (`g`, `j`, `p`, `q`, `y`) and tall punctuation (`Q`, `,`, `;`, `( )`, `[ ]`)
  - **Max glyph width**: `1–7 px` (up to `8 px` for wide symbols), so `width_px + 1px gap <= 8 px`.

## Colors (SNES 2bpp Compatibility)
- Draw glyph pixels in **pure white (`#FFFFFF`, alpha `255`)** on a **transparent (`alpha = 0`)** or **pure black (`#000000`)** background.
- Keep it monochrome (`transparent + white`) so Song Master's built-in palette color switches (`<FD:1F>` white, `<FD:20>` gold/speaker, `<FD:21>` cyan/location) tint your font cleanly.

## How Widths Are Read
- By default (`"auto_measure_width": true`), the importer detects each character's width from its **rightmost lit pixel** (`max_x + 1`) and adds `1px` spacing (`"default_spacing_px": 1`).
- `" "` (space) uses `"space_width_px": 3` (adjustable in `vwf_widths.json`).
- If you want a custom width override for any specific character (e.g., tighter spacing or monospace numbers `0-9`), set `"auto_measure_width": false` or edit that glyph's `"width_px"` / `"advance_px"` in `vwf_widths.json`.
