/*
 * Song Master — per-game runtime glue.
 *
 * THIS FILE IS THE PORT. Everything else the scaffold produced is layout,
 * build wiring, and packaging; this is where the actual work happens.
 *
 * The framework does not, and cannot, drive an arbitrary SNES game on its
 * own. Two responsibilities always land on the game host:
 *
 *   1. Deciding what "one frame" means for THIS title, and returning from
 *      run_frame() at that boundary. A real ROM's reset vector never
 *      returns — it enters a main loop that waits on vblank — so somebody
 *      has to choose the yield point.
 *   2. Delivering NMI/IRQ at the hardware edge (see
 *      snesrecomp/docs/FRAME_MODEL_HOSTS.md).
 *
 * A new port starts on the framework's beam-aligned driver
 * (runner/src/beam_frame_driver.h): one frame is one PPU field, cut at the
 * beam's vblank edge; interrupts -- raster splits and a coprocessor's IRQ
 * line -- are taken where the beam latches them; parked time still runs the
 * beam, APU and coprocessors; and the field is rasterized from the raster
 * journal with real HDMA. It is the framework's, so a fix to it reaches this
 * port on a submodule pull.
 *
 * This file used to hold its own copy of a simpler driver: frames measured
 * from wherever the CPU last stopped, NMI delivered at an arbitrary beam
 * position, the field drawn with end-of-frame register state. A game that
 * uploads in vblank between INIDISP=$80 and $0F flickered black, and one
 * that chains raster splits fell apart. Replace these calls only for what
 * the driver does not do; MetalWarriorsSNESRecomp's src/mw_rtl.c and
 * GundamWingEndlessDuelSNESRecomp's src/game_rtl.c are developed examples of
 * title-specific drivers.
 */

#include "game_rtl.h"

#include "beam_frame_driver.h"
#include "cpu_state.h"
#include "snes/interp_bridge.h"
#include "snes/snes.h"

extern CpuState g_cpu;
extern Snes *g_snes;

void GameRunOneFrame(void)
{
    /* Song Master's NMI handler ($00:836E -> $00:898C) polls $4212 bit 7
     * (BMI) until VBlank ends before reading joypads and returning. Because
     * the beam frame driver cuts at V=225 H=0 and delivers NMI immediately
     * on the next call, a main-thread VBlank wait ($00:C088: LDA $4212 /
     * ASL A / BCC) never observes VBlank=1 unless we allow a brief hardware
     * NMI-entry window (~64 master cycles) at V=225 first. */
    uint32_t pc = 0;
    bool wai = false;
    snes_beam_frame_driver_get_position(&pc, &wai);
    if (pc != 0 && !wai && g_snes->nmiEnabled && g_snes->vPos >= 225) {
        interp_bridge_set_master_deadline(g_cpu.master_cycles + 64);
        (void)interp_bridge_run_until_quiescent(&g_cpu, pc);
        interp_bridge_set_master_deadline(0);
        uint32_t next_pc = interp_bridge_lle_resume_pc();
        if (next_pc) {
            snes_beam_frame_driver_set_position(next_pc, interp_bridge_lle_took_wai());
        }
    }
    snes_beam_frame_driver_run_frame();
}

void GameDrawPpuFrame(void)
{
    snes_beam_frame_driver_draw_ppu_frame();
}

void GameSessionReset(void)
{
    /* Rematch / soft-return: clear anything that must not survive a new
     * session. recomp-ai-rules/NETPLAY.md §3 — sticky state that "has always
     * been fine" is the usual desync culprit, because single-player never
     * re-enters the boot path twice in one process. */
    snes_beam_frame_driver_reset();
}

const RtlGameInfo kGameInfo = {
    .title = "songmaster",
    .initialize = NULL,
    .run_frame = &GameRunOneFrame,
    .draw_ppu_frame = &GameDrawPpuFrame,
    .save_name_prefix = "save",
    .session_reset = &GameSessionReset,
    .hardware_reset = &GameSessionReset,
    .state_save_extra = &snes_beam_frame_driver_state_save,
    .state_load_extra = &snes_beam_frame_driver_state_load,
    .on_state_loaded = &snes_beam_frame_driver_state_loaded,
    .snapshot_guard_identity = &snes_beam_frame_driver_state_identity,
    .snapshot_preflight = &snes_beam_frame_driver_state_preflight,
};
