/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "aux_pio.h"

bool aux_pio_claim(AUX_PIO* aux, const pio_program_t* program)
{
    aux->claimed = false;

    //From the highest block down, never PIO0
    for(int index = NUM_PIOS - 1; index >= 1; index--)
    {
        PIO pio = pio_get_instance((uint)index);

        if(!pio_can_add_program(pio, program))
            continue;

        int sm = pio_claim_unused_sm(pio, false);
        if(sm < 0)
            continue;

        aux->pio = pio;
        aux->sm = (uint)sm;
        aux->offset = pio_add_program(pio, program);
        aux->program = program;
        aux->claimed = true;
        return true;
    }

    return false;
}

void aux_pio_release(AUX_PIO* aux)
{
    if(!aux->claimed)
        return;

    pio_sm_set_enabled(aux->pio, aux->sm, false);
    pio_sm_clear_fifos(aux->pio, aux->sm);
    pio_remove_program(aux->pio, aux->program, aux->offset);
    pio_sm_unclaim(aux->pio, aux->sm);
    aux->claimed = false;
}
