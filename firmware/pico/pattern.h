/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef __OPENSCILAB_PATTERN__
#define __OPENSCILAB_PATTERN__

//Pattern generator (commands 21-24): samples from the pattern buffer on up to 24 consecutive
//GPIOs, by a PIO state machine fed by two DMA channels (one restarts the other at the end of
//every pass, the state machine counts the samples).
//
//The pattern buffer is the start of the capture memory: what a pattern holds is not available
//to captures (GEN_LOAD with offset 0 and no data empties it). It holds up to 64 KiB on the
//RP2040 and 256 KiB on the RP2350.

#include "pico/stdlib.h"

#if defined (CORE_TYPE_2)
    #define PATTERN_BUFFER_MAX (256 * 1024)
#else
    #define PATTERN_BUFFER_MAX (64 * 1024)
#endif

//Most pins of the generator
#define PATTERN_MAX_PINS 24

//Flags of GEN_START
#define GEN_START_WAIT_TRIGGER 0x01

/// @brief The pattern buffer: the start of the capture memory
void pattern_init(uint8_t* memory, uint32_t size);

/// @brief Command 21
/// @param samples Set to the samples in the buffer
const char* pattern_load(uint32_t offset, uint8_t flags, const uint8_t* data, uint32_t length, uint32_t* samples);

/// @brief Command 22
/// @param actual Set to the actual sample rate
const char* pattern_start(float rate, uint8_t firstPin, uint8_t pinCount, uint32_t length, uint32_t passes, uint8_t flags, uint8_t syncPin, double* actual);

/// @brief Command 23 (and part of SAFE): stops the output, its pins become inputs
void pattern_stop(void);

/// @brief Command 24
void pattern_status(bool* running, uint32_t* passesDone);

/// @brief Notices the start of the output (main loop)
void pattern_step(void);

/// @brief Whether the generator drives its pins (running, or finished and holding the last sample)
bool pattern_active(void);

/// @brief Bytes at the start of the capture memory that the pattern holds
uint32_t pattern_reserved_bytes(void);

/// @brief Highest sample rate
uint32_t pattern_max_rate(void);

#endif
