/*
 * Song Master — runtime localization mod plugin.
 *
 * Registers the trusted plugin ID referenced by
 * mods/preloaded/packages/songmaster.translation/1.0.0/manifest.toml.
 */

#include "mod_runtime.h"
#include "snes_text_xlate.h"

#include <stdio.h>

static void activate_english_translation(void)
{
    if (!snes_text_xlate_init_c("translations/songmaster.toml", "en")) {
        fprintf(stderr, "[xlate] failed to load translations/songmaster.toml: %s\n",
                snes_text_xlate_last_error_c());
        return;
    }
    snes_mod_register_frame_callback(&snes_text_xlate_on_frame_c);
}

void songmaster_register_xlate_plugins(void)
{
    snes_mod_register_activation_plugin("songmaster.translation.en",
                                        &activate_english_translation);
}
