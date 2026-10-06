// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// State captures: an interrupt on the edges of the clock pin takes a frame (time in µs, the
// levels, the latest value of each analog channel). Frames go out as DATA events while the
// capture runs (live state); a full ring loses the edge and counts it (DONE "edges lost").

#include "app.h"
#include "board.h"
#include "capture_internal.h"

using namespace proto;

namespace capture {

static Ring frames;  // records <II time, levels, then <H per channel (DATA kind 2)
static bool state_triggered;
static uint32_t state_previous;
static uint32_t frames_sent;
static uint32_t state_last_send_ms;

FW_ISR static void edge() {
    if (ending) return;
    uint32_t time = hal::micros();
    uint32_t levels = hal::read_levels();
    if (!state_triggered) {
        bool hit = trigger_hit(state_previous, levels);
        state_previous = levels;
        if (!hit) return;
        state_triggered = true;
    }
    uint8_t *slot = frames.slot();
    if (!slot) {
        lost++;
        return;
    }
    put_u32(slot, time);
    put_u32(slot + 4, levels & config.mask);
    slot += 8;
    for (uint8_t channel = 0; channel < FW_ADC_CHANNELS; channel++) {
        if (config.adc & (1u << channel)) {
            put_u16(slot, app::adc_latest[channel]);
            slot += 2;
        }
    }
    frames.commit();
    taken++;
    if (config.samples && taken >= config.samples) finish(DONE_COMPLETE);
}

uint8_t state_start() {
    uint16_t record = (uint16_t)(8 + 2 * config.adc_count);
    if (record > DATA_ROOM) return ERR_ARGUMENTS;
    frames.init(memory, FW_CAPTURE_BYTES, record);
    state_triggered = config.trigger == TRIGGER_NONE;
    state_previous = hal::read_levels();
    frames_sent = 0;
    state_last_send_ms = hal::millis();
    phase = RUNNING;
    uint8_t error = hal::edge_attach(config.clock_pin, config.clock_edge == 1, edge);
    if (error) phase = IDLE;
    return error;
}

void state_stop() { hal::edge_detach(); }

void state_poll() {
    if (phase == RUNNING && ending) {
        state_stop();
        phase = DRAINING;
    }
    bool draining = phase == DRAINING;
    uint32_t now = hal::millis();
    uint16_t per_frame = (uint16_t)(DATA_ROOM / frames.record());
    uint16_t pending = frames.count();
    if (pending && (draining || pending >= per_frame || now - state_last_send_ms >= 20)) {
        if (pending > per_frame) pending = per_frame;
        app::event(EVENT_DATA);
        app::out.u8(DATA_STATE);
        app::out.u32(frames_sent);
        for (uint16_t i = 0; i < pending; i++) {
            app::out.bytes(frames.front(), frames.record());
            frames.pop();
        }
        frames_sent += pending;
        app::send();
        state_last_send_ms = now;
        return;
    }
    if (draining) send_done(frames_sent);
}

}  // namespace capture
