/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef __OPENSCILAB_AUX_PIO__
#define __OPENSCILAB_AUX_PIO__

//State machines of the outputs (pulse, pattern generator, UART and SPI transmitters).
//
//PIO0 belongs to the captures: they clear its instruction memory whenever they start. The
//outputs therefore use the other PIO blocks (PIO1 on the RP2040, PIO2 and PIO1 on the RP2350),
//which they share with the fast trigger capture (PIO1) and the WiFi chip of the W boards (the
//highest PIO block). A program stays loaded only while its output runs.

#include "pico/stdlib.h"
#include "hardware/pio.h"

typedef struct _AUX_PIO
{
    PIO pio;
    uint sm;
    uint offset;
    const pio_program_t* program;
    bool claimed;

} AUX_PIO;

/// @brief Claims a state machine and loads the program on a PIO block other than PIO0
/// @return False if no block has a free state machine and room for the program
bool aux_pio_claim(AUX_PIO* aux, const pio_program_t* program);

/// @brief Stops the state machine, removes the program and frees the state machine
void aux_pio_release(AUX_PIO* aux);

#endif
