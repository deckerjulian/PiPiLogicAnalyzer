// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// The fake board of the native tests: an Uno-like pin table (D0/D1 reserved, PWM, A0-A5 with a
// DAC on A0, square wave on D9/D10, clock pins), time and timers driven by the test.

#ifndef ARCH_NATIVE_FAKE_HAL_H
#define ARCH_NATIVE_FAKE_HAL_H

#if !defined(ARDUINO)

#include <stdint.h>

#include <vector>

#include "fast.h"

namespace hal {
namespace fake {

extern uint32_t now_us;
extern std::vector<uint8_t> line;            // bytes sent to the host
extern std::vector<uint32_t> writes;         // levels after each write_levels (pattern tests)
extern uint16_t adc_values[16];
extern uint32_t pullups;
extern uint32_t pulldowns;
extern uint32_t pwm_pins;
extern float pwm_frequency[32];
extern uint16_t pwm_duty[32];
extern uint32_t dac_pins;
extern int square_pin;
extern float square_frequency;
extern bool clock_busy;                      // the counted clock is in use
extern uint8_t edge_pin;
extern bool edge_falling;
extern void (*edge_handler)();
extern void (*timer_handler[2])();
extern float timer_rates[2];

void reset();
// runs the timer of a slot n times (and advances the time by its period)
void tick(uint8_t slot, uint32_t n);
// an edge on the clock pin
void edge();

}  // namespace fake
}  // namespace hal

#endif
#endif
