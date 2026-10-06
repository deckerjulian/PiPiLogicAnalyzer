// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// Outputs that run on their own: the pattern generator (runs played by a timer), the square wave
// and the arbitrary waveform on the DAC.

#ifndef GENERATOR_H
#define GENERATOR_H

#include "platform.h"

namespace gen {

uint8_t load(const uint8_t *data, uint8_t n, uint16_t *runs);       // GEN_LOAD
uint8_t start(const uint8_t *data, uint8_t n, float *actual);       // GEN_START
void stop();                                                        // GEN_STOP (pattern and DAC)
void status(uint8_t *running, uint16_t *passes);                    // GEN_STATUS
uint8_t square(uint8_t pin, float frequency, float *actual);        // SQUARE
uint8_t arb_load(const uint8_t *data, uint8_t n, uint16_t *points); // ARB_LOAD
uint8_t arb_start(const uint8_t *data, uint8_t n, float *actual);   // ARB_START

// pins the generator drives (pattern pins, the square pin, the DAC pin)
uint32_t pins();
// something runs that drives outputs
bool driving();
// stops everything (SAFE)
void stop_all();
// work between frames (the end of the last pass)
void poll();

}  // namespace gen

#endif
