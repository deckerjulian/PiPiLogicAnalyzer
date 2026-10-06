// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// Fast pin access on the Uno R4 (RA4M1): the pins sit on ports 0, 1 and 3; the levels are read
// from the port input registers and spread to the pin bits by a table made at start-up. The
// counted clock is the cycle counter of the Cortex-M4 (DWT).

#ifndef ARCH_RENESAS_FAST_H
#define ARCH_RENESAS_FAST_H

#include <Arduino.h>
#include <stdint.h>

namespace hal {

typedef uint32_t cycle_t;

namespace ra {
const uint8_t PORTS = 5;
extern R_PORT0_Type *const port[PORTS];
extern uint8_t pin_port[32];
extern uint16_t pin_mask[32];
extern uint8_t pin_count;
}  // namespace ra

static inline cycle_t cycles() { return DWT->CYCCNT; }

static inline uint32_t read_levels() {
    uint16_t in[ra::PORTS];
    for (uint8_t p = 0; p < ra::PORTS; p++) in[p] = ra::port[p]->PIDR;
    uint32_t levels = 0;
    for (uint8_t i = 0; i < ra::pin_count; i++)
        if (in[ra::pin_port[i]] & ra::pin_mask[i]) levels |= (uint32_t)1 << i;
    return levels;
}

static inline void write_levels(uint32_t mask, uint32_t levels) {
    uint16_t set[ra::PORTS] = {0}, reset[ra::PORTS] = {0};
    for (uint8_t i = 0; i < ra::pin_count && mask; i++) {
        uint32_t bit = (uint32_t)1 << i;
        if (!(mask & bit)) continue;
        mask &= ~bit;
        if (levels & bit) set[ra::pin_port[i]] |= ra::pin_mask[i];
        else reset[ra::pin_port[i]] |= ra::pin_mask[i];
    }
    for (uint8_t p = 0; p < ra::PORTS; p++) {
        if (set[p]) ra::port[p]->POSR = set[p];
        if (reset[p]) ra::port[p]->PORR = reset[p];
    }
}

static inline void dac_write_fast(uint16_t raw) { R_DAC->DADR[0] = raw; }

}  // namespace hal

#endif
