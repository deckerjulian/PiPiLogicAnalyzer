// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// What the three capture modes share (capture.cpp, capture_stream.cpp, capture_state.cpp,
// capture_buffer.cpp).

#ifndef CAPTURE_INTERNAL_H
#define CAPTURE_INTERNAL_H

#include "board_config.h"
#include "capture.h"
#include "protocol.h"
#include "ring.h"

namespace capture {

struct Config {
    bool valid;
    uint8_t mode;
    uint32_t rate;
    uint32_t mask;
    uint16_t adc;
    uint8_t adc_count;
    uint32_t samples;  // 0: until stopped (stream, state)
    uint32_t pre;
    uint8_t trigger;
    uint32_t trigger_mask;
    uint32_t trigger_levels;
    uint8_t clock_pin;
    uint8_t clock_edge;
    uint32_t period;   // buffer: ticks of the counted clock per sample
    uint32_t ticks;    // buffer: ticks per second of the counted clock
};

extern Config config;

// IDLE -> RUNNING -> (the sampling ends) DRAINING -> DONE sent -> IDLE
const uint8_t IDLE = 0;
const uint8_t RUNNING = 1;
const uint8_t DRAINING = 2;

extern volatile uint8_t phase;
extern volatile bool ending;        // set when the sampling should stop (interrupt or abort)
extern volatile uint8_t end_status; // DONE status
extern volatile uint32_t taken;     // samples (frames) taken
extern volatile uint32_t lost;      // state mode: edges lost
extern uint8_t memory[FW_CAPTURE_BYTES];

// the levels count as a trigger (prev: the levels before)
static inline bool trigger_hit(uint32_t prev, uint32_t now) {
    switch (config.trigger) {
        case proto::TRIGGER_RISING: return ((prev ^ now) & now & config.trigger_mask) != 0;
        case proto::TRIGGER_FALLING: return ((prev ^ now) & prev & config.trigger_mask) != 0;
        case proto::TRIGGER_PATTERN: return (now & config.trigger_mask) == config.trigger_levels;
        default: return true;
    }
}

static inline void finish(uint8_t status) {
    if (!ending) {
        end_status = status;
        ending = true;
    }
}

// sends DONE and returns to IDLE
void send_done(uint32_t samples);

uint8_t stream_start();
void stream_stop();   // the sampling stops (from the main loop)
void stream_poll();

uint8_t state_start();
void state_stop();
void state_poll();

uint8_t buffer_setup(uint32_t *actual);
uint8_t buffer_start();
void buffer_stop();
void buffer_poll();

// payload room for records behind the DATA header (event, kind, index)
const uint8_t DATA_ROOM = (uint8_t)(proto::MAX_PAYLOAD - 6);

}  // namespace capture

#endif
