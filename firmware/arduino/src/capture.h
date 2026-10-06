// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// Captures: stream (timer interrupt, digital as runs, analog alongside), buffer (counted loop
// into RAM, pre-trigger by a ring) and state (an interrupt on the edges of a clock pin).

#ifndef CAPTURE_H
#define CAPTURE_H

#include "platform.h"

namespace capture {

// CAPTURE_SETUP: <BIIHIIBIIBB>; answers the actual rate
uint8_t setup(const uint8_t *data, uint8_t n, uint32_t *actual);
// CAPTURE_START
uint8_t start();
// CAPTURE_ABORT (the event DONE follows)
void abort();
// a capture runs (or still sends its data)
bool running();
// pins of the running capture (channels, trigger, clock)
uint32_t pins();
// analog channels of the running capture
uint16_t adc_mask();
// a buffer capture runs on a board whose GPIO commands must wait (AVR)
bool gpio_locked();
// sends data and DONE; runs buffer captures
void poll();

}  // namespace capture

#endif
