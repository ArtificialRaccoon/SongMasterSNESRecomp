# Song Master Recomp

A native recompilation of **Song Master** (JPN), built on
[snesrecomp](https://github.com/RetroPortingToolKit/snesrecomp).

## About...

I coded absolutely nothing in this repo.  It is entirely AI translated, and
AI coded.  I simply wanted to play Song Master in English, and since no one
seemed to be interested in the game, I figured why not let an AI handle it.

The translation hasn't been proofed, so it's likely very flat sounding.

Then again, outside of Ted Woolsey, most translations back in the day were
stilted, to say the least.

Feel free to fork this and give it a real translation if you're so inclined.

All the docs are AI written as well, so don't expect them to make sense...

Builds both an SNESRecomp executable, and a functioning SNES ROM (tested in
bsnes).

> **You must legally own a copy of the game.** No ROM data is distributed with
> this project, in the repository or in any release. The recompiled C is
> generated locally from your own copy and is never committed.

## ROM identity

| | |
|---|---|
| File | `Song Master (Japan).sfc` |
| Publisher | — |
| Developer | — |
| Year | — |
| Mapping | lorom |
| Region | JPN (Japan) |
| Coprocessor | none |
| CRC32 | `4fa5fc25` |
| SHA-256 | `c9775382e46ff3187dc2630ee290a56f75a8a6b75d448ccbf9204d8b50dbb7b4` |

`tools/regen.sh` refuses to run against anything else, so a mismatched
revision fails immediately instead of producing subtly wrong output.

## Build

```sh
git submodule update --init --recursive
bash tools/regen.sh --rom /path/to/Song Master (Japan).sfc
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
```

The ROM does not have to live in the repository — keeping it on your own
drive (or under `ROM/`) is supported, and `SNESRECOMP_ROM` sets the path once
for a shell. Without either, `tools/regen.sh` checks the repo root, the path
recorded in `rom.cfg`, and `ROM/`; `.gitignore` blocks all `*.sfc` and
`rom.cfg` files from ever being committed.

`tools/regen.sh` verifies the ROM, generates `src/gen/*.c`, and re-syncs
`recomp/funcs.h`. Re-run it whenever you change anything under `recomp/`.

## Run

```sh
./build/SongMasterSNESRecomp                     # launcher picks the ROM
./build/SongMasterSNESRecomp /path/to/Song Master (Japan).sfc # or name it and skip the launcher
```

With no ROM on the command line the build opens the recomp-ui launcher: a ROM
picker with a verification badge, plus display / audio / input settings. Your
choice is cached in `rom.cfg` beside the executable, so the next launch opens
on it and "Skip launcher on boot" makes it immediate. If the launcher is not
built in (`--no-recomp-ui`), the host falls back to a native file picker.

Either way the ROM is checked against the digests above before anything boots
— a dump that could not have produced this build is refused at the door rather
than mis-executing ten frames in.

## Building a Standalone English SNES ROM

In addition to running the translated game through the native `SongMasterSNESRecomp`
host, you can bake the English translation, script/menu fixes, and 65816
Variable-Width Font (VWF) engine directly into a standalone 2 MB LoROM `.sfc`
file playable on standard SNES emulators (such as `bsnes`) or flash carts.

1. Place your verified Japanese dump at `ROM/Song Master (Japan).sfc` (all
   `*.sfc`, `*.smc`, and `*.srm` files are ignored by `.gitignore`).
2. *(Optional)* If you edit `font/Eteran.png`, regenerate the VWF font tables
   and binary (requires Python 3 + `Pillow`):

   ```sh
   python font/build_vwf_font.py
   ```

3. Build the patched SNES ROM (Python 3 standard library only):

   ```sh
   python tools/build_patched_rom.py
   ```

By default, `tools/build_patched_rom.py` reads `ROM/Song Master (Japan).sfc`,
applies all `[[rom_patch]]` entries from `translations/songmaster.toml`
(translated script, reflowed line breaks, menu/naming fixes, crash fixes, and
1bpp lowercase menu glyphs), assembles the Bank `$20` (`$20:8000`) 65816 VWF
renderer and hooks using `translations/vwf_font.bin`, expands the ROM to 2 MB,
recomputes the SNES checksum, and writes `ROM/Song Master (English VWF).sfc`.

You can override any input or output path via CLI flags:

```sh
python tools/build_patched_rom.py \
  --src "/path/to/Song Master (Japan).sfc" \
  --out "/path/to/Song Master (English VWF).sfc" \
  --toml translations/songmaster.toml \
  --font-bin translations/vwf_font.bin
```

> If you run the standalone patched ROM inside `SongMasterSNESRecomp` instead of
> a standard SNES emulator, set `SNESRECOMP_NO_MODS=1` and
> `SONGMASTER_DISABLE_HOST_VWF=1` so the host does not double-apply the runtime
> translation or host VWF patches on top of an already-patched ROM.

## Layout

| Path | What lives there |
|---|---|
| `ROM/` | Local ROM directory for `Song Master (Japan).sfc` and generated `.sfc` builds (gitignored) |
| `font/` | `Eteran.png` VWF bitmap sheet, `vwf_widths.json`, and `build_vwf_font.py` |
| `translations/` | `songmaster.toml` patches, `vwf_font.bin`, and `script_proofread.csv` |
| `recomp/` | Analysis input: `bank*.cfg`, `symbols.toml`, generated `funcs.h` |
| `rom_identity.txt` | ROM digests — read by the build, `tools/regen.sh` and CI |
| `src/` | Host code you own: `main.c`, `game_rtl.c`, `vwf_dialog.c` |
| `src/gen/` | Generated C. Never committed — regenerate locally |
| `snesrecomp/` | Framework submodule (owns `lib/recomp-net`, `lib/retcomm-rbengine`) |
| `tools/` | `regen.sh` (ROM → C pipeline) and `build_patched_rom.py` (standalone `.sfc` builder) |
| `scripts/` | `package_release.sh` — player-facing zip |
| `framework_pins.txt` | Exact framework commits this project was scaffolded against |

## Porting from here

The scaffold stops where the game-specific work starts. In rough order:

1. **Make it boot.** `src/game_rtl.c` holds the frame driver. It starts on
   the framework's beam-aligned driver (`snesrecomp/runner/src/beam_frame_driver.h`):
   one frame per PPU field, interrupts taken where the beam latches them,
   the field rasterized from the raster journal. A title whose main loop it
   does not fit replaces those calls with its own driver.
2. **Name things.** Add entries to `recomp/symbols.toml` as you identify
   routines, then re-run `tools/regen.sh`. Set `emit = true` to promote one
   into ahead-of-time analysis; leave it false to keep it interpreted.
   Regen synchronizes the marked blocks in each bank cfg, creating missing
   configs. Only proven variants become AOT code. See `recomp/README.md`.
3. **Resolve dispatch misses.** After every run, deal with unresolved
   indirect targets before anything else — they are the reason a port
   diverges, and they are cheap to fix early.
4. **Never synthesise a result** to get past uncovered code, and never edit
   `src/gen/` by hand. Fix the config or the framework and regenerate.

## Multiplayer

Single player.

## License

This project's own source is under the license in `LICENSE`. The framework
carries its own terms — see `snesrecomp/LICENSE` and
`snesrecomp/THIRD_PARTY_ATTRIBUTION.md`. Neither covers the game data, which
is not distributed here.
