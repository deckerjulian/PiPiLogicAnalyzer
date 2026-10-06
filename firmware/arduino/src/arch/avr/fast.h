// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// Fast pin access on the AVR boards. The pin tables follow the port bits, so the levels of all
// pins are a few port reads:
//   Uno, Nano (ATmega328P): pins 0-7 PORTD (D0-D7), 8-13 PORTB (D8-D13), 14-19 PORTC (A0-A5)
//   Mega 2560:              pins 0-7 PORTA, 8-15 PORTB, 16-23 PORTL, 24-31 PORTK (A8-A15)

#ifndef ARCH_AVR_FAST_H
#define ARCH_AVR_FAST_H

#include <avr/io.h>
#include <stdint.h>

namespace hal {

// the counted clock is Timer1
typedef uint16_t cycle_t;
static inline cycle_t cycles() { return TCNT1; }

static inline void port_write(volatile uint8_t &port, uint8_t mask, uint8_t levels) {
    if (mask) port = (uint8_t)((port & ~mask) | (levels & mask));
}

#if defined(FW_BOARD_MEGA2560)

static inline uint32_t read_levels() {
    return (uint32_t)PINA | ((uint32_t)PINB << 8) | ((uint32_t)PINL << 16) | ((uint32_t)PINK << 24);
}

static inline void write_levels(uint32_t mask, uint32_t levels) {
    port_write(PORTA, (uint8_t)mask, (uint8_t)levels);
    port_write(PORTB, (uint8_t)(mask >> 8), (uint8_t)(levels >> 8));
    port_write(PORTL, (uint8_t)(mask >> 16), (uint8_t)(levels >> 16));
    port_write(PORTK, (uint8_t)(mask >> 24), (uint8_t)(levels >> 24));
}

#else

static inline uint32_t read_levels() {
    return (uint32_t)PIND | ((uint32_t)(PINB & 0x3F) << 8) | ((uint32_t)(PINC & 0x3F) << 14);
}

static inline void write_levels(uint32_t mask, uint32_t levels) {
    port_write(PORTD, (uint8_t)mask, (uint8_t)levels);
    port_write(PORTB, (uint8_t)((mask >> 8) & 0x3F), (uint8_t)(levels >> 8));
    port_write(PORTC, (uint8_t)((mask >> 14) & 0x3F), (uint8_t)(levels >> 14));
}

#endif

static inline void dac_write_fast(uint16_t) {}  // no DAC

}  // namespace hal

#endif
