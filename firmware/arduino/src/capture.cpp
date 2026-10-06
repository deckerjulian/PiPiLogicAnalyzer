// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// CAPTURE_SETUP / START / ABORT and what the modes share.

#include "app.h"
#include "board.h"
#include "capture_internal.h"
#include "generator.h"

using namespace proto;

namespace capture {

Config config;
volatile uint8_t phase = IDLE;
volatile bool ending = false;
volatile uint8_t end_status = DONE_COMPLETE;
volatile uint32_t taken = 0;
volatile uint32_t lost = 0;
uint8_t memory[FW_CAPTURE_BYTES];

static uint8_t count_bits(uint32_t mask) {
    uint8_t n = 0;
    for (; mask; mask &= mask - 1) n++;
    return n;
}

// pins a capture may read: they exist, are digital inputs and not reserved
static uint8_t check_inputs(uint32_t mask) {
    for (uint8_t i = 0; i < 32; i++) {
        if (!(mask & app::pin_bit(i))) continue;
        if (i >= board::PIN_COUNT) return ERR_ARGUMENTS;
        board::PinDef def = board::pin(i);
        if (def.reserved != board::RES_FREE) return ERR_RESERVED;
        if (!(def.caps & board::PC_DIN)) return ERR_UNSUPPORTED;
    }
    return 0;
}

uint8_t setup(const uint8_t *d, uint8_t n, uint32_t *actual) {
    if (n != 30) return ERR_ARGUMENTS;
    if (phase != IDLE) return ERR_BUSY;
    Config c;
    c.valid = false;
    c.mode = d[0];
    c.rate = get_u32(d + 1);
    c.mask = get_u32(d + 5);
    c.adc = get_u16(d + 9);
    c.samples = get_u32(d + 11);
    c.pre = get_u32(d + 15);
    c.trigger = d[19];
    c.trigger_mask = get_u32(d + 20);
    c.trigger_levels = get_u32(d + 24) & c.trigger_mask;
    c.clock_pin = d[28];
    c.clock_edge = d[29];
    c.adc_count = count_bits(c.adc);
    c.period = c.ticks = 0;

    if (c.mode > MODE_STATE || c.trigger > TRIGGER_PATTERN || c.clock_edge > 1) return ERR_ARGUMENTS;
    if (!c.mask && !c.adc) return ERR_ARGUMENTS;
    uint8_t error = check_inputs(c.mask);
    if (error) return error;
    uint8_t analog = board::analog_count();
    if (analog < 16 && (c.adc >> analog)) return ERR_ARGUMENTS;
    if (c.trigger != TRIGGER_NONE) {
        if (!c.trigger_mask) return ERR_ARGUMENTS;
        error = check_inputs(c.trigger_mask);
        if (error) return error;
    }
    if (c.mode != MODE_BUFFER && c.pre) return ERR_UNSUPPORTED;  // pre-trigger: buffer captures only

    config = c;
    switch (c.mode) {
        case MODE_STREAM: {
            if (c.rate == 0 || c.rate > board::INFO.stream_max_rate) return ERR_ARGUMENTS;
            *actual = (uint32_t)(hal::timer_rate((float)c.rate) + 0.5f);
            break;
        }
        case MODE_STATE: {
            if (c.clock_pin >= board::PIN_COUNT) return ERR_ARGUMENTS;
            board::PinDef def = board::pin(c.clock_pin);
            if (def.reserved != board::RES_FREE) return ERR_RESERVED;
            if (!(def.caps & (board::PC_CLOCK | board::PC_CLOCK_SW))) return ERR_UNSUPPORTED;
            *actual = 0;
            break;
        }
        default:
            error = buffer_setup(actual);
            if (error) return error;
    }
    config.valid = true;
    return 0;
}

uint8_t start() {
    if (phase != IDLE) return ERR_BUSY;
    if (!config.valid) return ERR_ARGUMENTS;
    if (gen::pins() & (config.mask | config.trigger_mask)) return ERR_BUSY;
    ending = false;
    end_status = DONE_COMPLETE;
    taken = 0;
    lost = 0;
    switch (config.mode) {
        case MODE_STREAM: return stream_start();
        case MODE_STATE: return state_start();
        default: return buffer_start();
    }
}

void abort() {
    if (phase != RUNNING) return;
    finish(DONE_STOPPED);
    switch (config.mode) {
        case MODE_STREAM: stream_stop(); break;
        case MODE_STATE: state_stop(); break;
        default: buffer_stop();
    }
}

bool running() { return phase != IDLE; }

uint32_t pins() {
    if (phase == IDLE) return 0;
    uint32_t mask = config.mask | (config.trigger ? config.trigger_mask : 0);
    if (config.mode == MODE_STATE) mask |= app::pin_bit(config.clock_pin);
    return mask;
}

uint16_t adc_mask() { return phase == RUNNING ? config.adc : 0; }

bool gpio_locked() { return phase == RUNNING && config.mode == MODE_BUFFER && board::INFO.gpio_locked_in_buffer; }

void send_done(uint32_t samples) {
    app::event(EVENT_DONE);
    app::out.u8(end_status);
    app::out.u32(samples);
    app::out.u32(lost);
    app::send();
    phase = IDLE;
}

void poll() {
    if (phase == IDLE) return;
    switch (config.mode) {
        case MODE_STREAM: stream_poll(); break;
        case MODE_STATE: state_poll(); break;
        default: buffer_poll();
    }
}

}  // namespace capture
