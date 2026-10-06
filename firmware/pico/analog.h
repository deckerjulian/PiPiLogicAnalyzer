/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef __OPENSCILAB_ANALOG__
#define __OPENSCILAB_ANALOG__

//The ADC: single readings (command 19, monitor) and analog channels of a capture or stream
//(command 25). During a capture the ADC converts the inputs of the mask in turn (round robin,
//500 kSa/s in total at most) and two DMA channels write them into a ring in the capture memory;
//the samples are interleaved in the order of the mask bits.

#include "pico/stdlib.h"

/// @brief Sets up the ADC and the DMA interrupt (once at start-up)
void analog_init(void);

/// @brief Command 25: analog channels of the next capture request or stream
/// @param mask ADC inputs (bit n: ADC n), 0: none
/// @param rate Rate per channel, Hz (0: none)
/// @param actualMilliHz Set to the actual rate per channel in mHz (0 without analog channels)
/// @return NULL or the error answer
const char* analog_configure(uint8_t mask, uint32_t rate, uint32_t* actualMilliHz);

/// @brief The next capture or stream takes the configuration (it applies once)
/// @return True if analog channels are configured
bool analog_take(void);

/// @brief Inputs, their number and the rate (mHz) of the configuration
uint8_t analog_mask(void);
uint8_t analog_channels(void);
uint32_t analog_rate_milli_hz(void);

/// @brief GPIOs of the configured inputs
uint32_t analog_gpio_mask(void);

/// @brief GPIOs of the running analog capture (0 if none runs)
uint32_t analog_busy_gpio_mask(void);

/// @brief Starts the conversions of the configuration into a ring (a multiple of the channels)
/// @return False if no DMA channel is free
bool analog_start(uint16_t* ring, uint32_t ringSamples);

/// @brief The capture ended: stop converting (safe in interrupt handlers)
void analog_capture_ended(void);

/// @brief Stops the conversions, waits for the last samples and frees the DMA channels
void analog_stop(void);

/// @brief Whether the conversions of a capture or stream run (or ran and were not stopped)
bool analog_active(void);

/// @brief Samples written into the ring so far (live: the one in transfer is not counted)
uint64_t analog_written(bool live);

/// @brief The ring of the running capture
const uint16_t* analog_ring(uint32_t* samples);

/// @brief Readings of the inputs of a mask in the order of the bits; during an analog capture
/// its newest samples (inputs it does not convert: their last single reading)
/// @param strict True: ERR:BUSY for an input the running capture does not convert
/// @return NULL or the error answer
const char* analog_read(uint8_t mask, uint16_t* values, uint8_t* count, bool strict);

/// @brief Sends the analog samples of a buffer capture (after its digital data)
/// @param digitalSamples Samples of the digital capture
/// @param digitalRate Its sample rate, Hz
/// @param send Transfer function of the connection
void analog_send_capture(uint32_t digitalSamples, uint32_t digitalRate, void (*send)(const uint8_t* data, uint32_t length));

#endif
