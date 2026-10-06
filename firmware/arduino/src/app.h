// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// The firmware without the hardware: frames in, commands, answers and events out. main.cpp feeds
// it the bytes of the line and calls poll() in its loop; the native tests do the same against
// the fake board.

#ifndef APP_H
#define APP_H

#include "board_config.h"
#include "protocol.h"

namespace app {

void init();
// one byte from the host
void receive(uint8_t byte);
// the work between frames: monitor reports, capture data, watchdog
void poll();

// --- for the modules (capture, generator, tx)

// the frame being built (an answer, an error answer or an event)
extern proto::Writer out;
// starts an event frame with its code
void event(uint8_t code);
// sends the frame in out
void send();

// 0 when every pin of mask exists, is free and has all of caps; else the error code
uint8_t check_pins(uint32_t mask, uint16_t caps);
// the pins become outputs that are driven (SAFE and the watchdog release them)
void driving(uint32_t mask);
// the pins are no longer driven by a command
void released(uint32_t mask);

// the latest raw value of each analog channel the monitor or a capture uses (refreshed in turn)
extern volatile uint16_t adc_latest[FW_ADC_CHANNELS];

// releases every output (command SAFE, the watchdog)
void safe();

static inline uint32_t pin_bit(uint8_t n) { return (uint32_t)1 << n; }

}  // namespace app

#endif
