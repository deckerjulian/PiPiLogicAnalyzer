// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// Buffer captures: a loop paced by the counted clock reads the pins into RAM (1 to 4 bytes per
// sample, by the highest pin of the mask). While it waits for the trigger it writes a ring and
// returns to the main loop now and then (answers, abort); once the pre-trigger samples are in
// and the trigger hits, it records the rest without a break. Then the samples go out as runs.
// A sample the loop could not take in time ends the capture with DONE status overflow.

#include "app.h"
#include "board.h"
#include "capture_internal.h"
#include "generator.h"

using namespace proto;

namespace capture {

static const uint8_t ARMED = 0;    // waiting for the trigger
static const uint8_t SENDING = 1;  // sending the samples
static uint8_t stage;
static uint8_t width;              // bytes per sample
static uint32_t total;             // samples in the buffer
static uint32_t first;             // where the first sample is in the ring
static uint32_t cursor;            // samples sent
static bool late;

static const hal::cycle_t HALF = (hal::cycle_t)(((hal::cycle_t)~(hal::cycle_t)0) / 2 + 1);

static uint8_t width_of(uint32_t mask) {
    if (mask <= 0xFFu) return 1;
    if (mask <= 0xFFFFu) return 2;
    if (mask <= 0xFFFFFFu) return 3;
    return 4;
}

static inline void store(uint32_t index, uint32_t levels) {
    uint8_t *p = memory + index * width;
    p[0] = (uint8_t)levels;
    if (width > 1) p[1] = (uint8_t)(levels >> 8);
    if (width > 2) p[2] = (uint8_t)(levels >> 16);
    if (width > 3) p[3] = (uint8_t)(levels >> 24);
}

static inline uint32_t load(uint32_t index) {
    const uint8_t *p = memory + index * width;
    uint32_t levels = p[0];
    if (width > 1) levels |= (uint32_t)p[1] << 8;
    if (width > 2) levels |= (uint32_t)p[2] << 16;
    if (width > 3) levels |= (uint32_t)p[3] << 24;
    return levels;
}

uint8_t buffer_setup(uint32_t *actual) {
    Config &c = config;
    if (c.adc) return ERR_UNSUPPORTED;  // the counted loop reads pins only
    if (c.rate == 0 || c.rate > board::INFO.buffer_max_rate) return ERR_ARGUMENTS;
    uint32_t capacity = FW_CAPTURE_BYTES / width_of(c.mask);
    if (c.samples == 0 || c.pre >= c.samples) return ERR_ARGUMENTS;
    if (c.samples > capacity) return ERR_OVERFLOW;
    // the loop does not answer while it records behind the trigger: two seconds at most
    if ((c.samples - c.pre) > 2 * c.rate) return ERR_ARGUMENTS;
    c.ticks = hal::clock_ticks((float)c.rate);
    if (!c.ticks) return ERR_UNSUPPORTED;
    c.period = (uint32_t)(((float)c.ticks / (float)c.rate) + 0.5f);
    if (c.period == 0 || c.period >= (uint32_t)HALF) return ERR_ARGUMENTS;
    *actual = (uint32_t)((float)c.ticks / (float)c.period + 0.5f);
    return 0;
}

uint8_t buffer_start() {
    if (board::INFO.gpio_locked_in_buffer && gen::driving()) return ERR_BUSY;  // its timer would disturb
    uint32_t ticks;
    uint8_t error = hal::clock_begin((float)config.rate, &ticks);
    if (error) return error;
    if (ticks != config.ticks) {
        hal::clock_end();
        return ERR_BUSY;
    }
    width = width_of(config.mask);
    total = config.samples;
    cursor = 0;
    late = false;
    stage = ARMED;
    phase = RUNNING;
    return 0;
}

void buffer_stop() {
    if (stage == ARMED) hal::clock_end();
}

// records the samples behind the trigger (index: where the trigger sample went)
static void record(uint32_t index, hal::cycle_t next) {
    hal::cycle_t period = (hal::cycle_t)config.period;
    uint32_t mask = config.mask;
    uint32_t remaining = total - config.pre - 1;
    hal::quiet_begin();
    while (remaining--) {
        hal::cycle_t now = hal::cycles();
        if ((hal::cycle_t)(now - next) < HALF && (hal::cycle_t)(now - next) >= period) late = true;
        while ((hal::cycle_t)(hal::cycles() - next) >= HALF) {
        }
        next += period;
        index = index + 1 == total ? 0 : index + 1;
        store(index, hal::read_levels() & mask);
    }
    hal::quiet_end((uint32_t)((uint64_t)(total - config.pre) * 1000000u / config.rate));
}

// waits for the trigger a while; true when the capture is recorded
static bool wait_for_trigger() {
    hal::cycle_t period = (hal::cycle_t)config.period;
    uint32_t mask = config.mask;
    uint32_t pre = config.pre;
    // a turn takes about 20 ms (and holds the pre-trigger samples twice): the ring of a turn is
    // contiguous, so the pre-trigger samples are those of this turn
    uint32_t budget = config.rate / 50;
    if (budget < 2 * pre + 64) budget = 2 * pre + 64;
    uint32_t index = 0, filled = 0;
    uint32_t previous = hal::read_levels();
    hal::cycle_t next = (hal::cycle_t)(hal::cycles() + period);
    while (budget--) {
        while ((hal::cycle_t)(hal::cycles() - next) >= HALF) {
        }
        next += period;
        uint32_t levels = hal::read_levels();
        store(index, levels & mask);
        if (filled >= pre && trigger_hit(previous, levels)) {
            first = (index + total - pre) % total;
            record(index, next);
            return true;
        }
        if (filled < pre) filled++;
        previous = levels;
        index = index + 1 == total ? 0 : index + 1;
    }
    return false;
}

static void send_samples() {
    const uint16_t per_frame = DATA_ROOM / 6;
    app::event(EVENT_DATA);
    app::out.u8(DATA_DIGITAL);
    app::out.u32(cursor);
    for (uint16_t runs = 0; runs < per_frame && cursor < total; runs++) {
        uint32_t index = (first + cursor) % total;
        uint32_t levels = load(index);
        uint16_t count = 0;
        while (cursor < total && count < 0xFFFF && load((first + cursor) % total) == levels) {
            cursor++;
            count++;
        }
        app::out.u16(count);
        app::out.u32(levels);
    }
    app::send();
}

void buffer_poll() {
    if (phase == RUNNING && ending) {  // aborted while it waits
        phase = DRAINING;
        send_done(0);
        return;
    }
    if (phase == RUNNING && stage == ARMED) {
        if (!wait_for_trigger()) return;
        hal::clock_end();
        stage = SENDING;
        phase = DRAINING;
        if (late) end_status = DONE_OVERFLOW;
        return;
    }
    if (cursor < total) {
        send_samples();
        return;
    }
    send_done(total);
}

}  // namespace capture
