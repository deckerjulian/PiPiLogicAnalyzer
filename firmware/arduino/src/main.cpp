// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// openSciLab Arduino firmware: the board as an instrument (GPIO, monitor, captures, pattern
// generator, sending protocols) behind the framed protocol of docs/protocols.md.

#if defined(ARDUINO)

#include <Arduino.h>

#include "app.h"
#include "board.h"
#include "hal.h"

void setup() {
    hal::init();  // every pin an input
    hal::serial_begin(board::INFO.serial_baud);
    app::init();
}

void loop() {
    // the bytes that are there, then the work between frames
    for (uint8_t n = 0; n < 64; n++) {
        int byte = hal::serial_read();
        if (byte < 0) break;
        app::receive((uint8_t)byte);
    }
    app::poll();
}

#endif
