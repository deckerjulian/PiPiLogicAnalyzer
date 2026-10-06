/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef __OPENSCILAB_PATTERN_DATA__
#define __OPENSCILAB_PATTERN_DATA__

//Data of the pattern generator (command 21 GEN_LOAD): raw samples or run lengths (pairs of a 16
//bit count and one sample), written into the pattern buffer from a sample offset on.
//
//Plain C without Pico SDK dependencies, compiled on the host by tests/ as well.

#include <stdint.h>
#include <stdbool.h>

//Flags of GEN_LOAD
#define GEN_LOAD_RUNS 0x01          //The data are run lengths
#define GEN_LOAD_WIDTH_SHIFT 1      //Bits 1-2: bytes per sample, 0: 1 byte, 1: 2 bytes, 2: 4 bytes
#define GEN_LOAD_WIDTH_MASK 0x06
#define GEN_LOAD_FLAGS_KNOWN (GEN_LOAD_RUNS | GEN_LOAD_WIDTH_MASK)

typedef enum
{
    GEN_DATA_OK = 0,
    GEN_DATA_FORMAT = -1,   //Malformed data (length, a run of 0 samples, unknown flags)
    GEN_DATA_FULL = -2      //The samples do not fit into the buffer

} GEN_DATA_RESULT;

/// @brief Bytes per sample of the GEN_LOAD flags (1, 2 or 4; 0 for an invalid code)
uint8_t gen_data_width(uint8_t flags);

/// @brief Bytes per sample for a pin count (1 for up to 8 pins, 2 for up to 16, 4 for more)
uint8_t gen_data_width_for_pins(uint8_t pins);

/// @brief Writes the data of one GEN_LOAD into the buffer; nothing is written if it fails
/// @param buffer Pattern buffer
/// @param capacity Size of the buffer in bytes
/// @param offset First sample to write
/// @param flags GEN_LOAD flags (run lengths, sample width)
/// @param data The data of the request
/// @param length Bytes of data
/// @param end Set to the sample after the last one written
/// @return GEN_DATA_OK or an error
GEN_DATA_RESULT gen_data_load(uint8_t* buffer, uint32_t capacity, uint32_t offset, uint8_t flags, const uint8_t* data, uint32_t length, uint32_t* end);

#endif
