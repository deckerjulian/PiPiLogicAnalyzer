// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// The pattern generator (runs <HI count, levels in RAM, played by a timer interrupt on the
// pins of a mask), the square wave (a hardware timer) and the arbitrary waveform (DAC points
// played by a timer interrupt).

#include "generator.h"

#include "app.h"
#include "board.h"
#include "capture.h"
#include "hal.h"

using namespace proto;

namespace gen {

static uint8_t runs[FW_GEN_RUNS * 6];
static uint16_t runs_loaded = 0;
#if FW_ARB_POINTS
static uint16_t points[FW_ARB_POINTS];
static uint16_t points_loaded = 0;
static uint16_t wave_points = 0;
#endif

// what plays
static const uint8_t NOTHING = 0;
static const uint8_t PATTERN = 1;
static const uint8_t WAVE = 2;
static volatile uint8_t playing = NOTHING;
static volatile bool finished = false;  // the last pass is over (the timer stops in poll)
static volatile uint16_t passes_done = 0;
static uint16_t passes_wanted = 0;
static uint32_t pattern_mask = 0;
static uint16_t index_now = 0;
static uint16_t remaining = 0;

static uint8_t square_pin = 0xFF;

FW_ISR static void pattern_tick() {
    if (finished) return;
    if (--remaining) return;
    if (++index_now == runs_loaded) {
        index_now = 0;
        passes_done++;
        if (passes_wanted && passes_done >= passes_wanted) {
            finished = true;
            return;
        }
    }
    const uint8_t *run = runs + (uint32_t)index_now * 6;
    remaining = get_u16(run);
    hal::write_levels(pattern_mask, get_u32(run + 2));
}

#if FW_ARB_POINTS
FW_ISR static void wave_tick() {
    if (finished) return;
    if (++index_now == wave_points) {
        index_now = 0;
        passes_done++;
        if (passes_wanted && passes_done >= passes_wanted) {
            finished = true;
            return;
        }
    }
    hal::dac_write_fast(points[index_now]);
}
#endif

uint8_t load(const uint8_t *d, uint8_t n, uint16_t *loaded) {
    if (n < 2 || (n - 2) % 6) return ERR_ARGUMENTS;
    if (playing == PATTERN) return ERR_BUSY;
    uint16_t offset = get_u16(d);
    uint16_t count = (uint16_t)((n - 2) / 6);
    if (offset > runs_loaded) return ERR_ARGUMENTS;  // no gaps
    if ((uint32_t)offset + count > FW_GEN_RUNS) return ERR_OVERFLOW;
    for (uint16_t i = 0; i < count; i++)
        if (get_u16(d + 2 + 6 * i) == 0) return ERR_ARGUMENTS;
    memcpy(runs + (uint32_t)offset * 6, d + 2, (size_t)count * 6);
    runs_loaded = (uint16_t)(offset + count);
    *loaded = runs_loaded;
    return 0;
}

static void halt() {
    if (playing != NOTHING) hal::timer_stop(hal::TIMER_OUTPUT);
    playing = NOTHING;
    finished = false;
}

uint8_t start(const uint8_t *d, uint8_t n, float *actual) {
    if (n != 11) return ERR_ARGUMENTS;
    float rate = get_f32(d);
    uint32_t mask = get_u32(d + 4);
    uint16_t passes = get_u16(d + 8);
    uint8_t flags = d[10];
    if (flags & 1) return ERR_UNSUPPORTED;  // no trigger input is defined for the Arduino boards
    if (!(rate > 0.0f) || rate > (float)board::INFO.generator_max_rate || !mask || !runs_loaded)
        return ERR_ARGUMENTS;
    if (playing != NOTHING) return ERR_BUSY;
    uint8_t error = app::check_pins(mask, board::PC_DOUT);
    if (error) return error;
    pattern_mask = mask;
    passes_wanted = passes;
    passes_done = 0;
    index_now = 0;
    finished = false;
    remaining = get_u16(runs);
    for (uint8_t i = 0; i < board::PIN_COUNT; i++)
        if (mask & app::pin_bit(i)) hal::pin_output(i, (get_u32(runs + 2) & app::pin_bit(i)) != 0);
    app::released(mask);  // the generator owns them now
    playing = PATTERN;
    error = hal::timer_start(hal::TIMER_OUTPUT, rate, pattern_tick, actual);
    if (error) {
        playing = NOTHING;
        return error;
    }
    return 0;
}

void stop() {
    if (playing == PATTERN) app::driving(pattern_mask);  // the pins keep their levels
    if (playing == WAVE) app::driving(app::pin_bit(board::dac_pin()));
    halt();
}

void status(uint8_t *running, uint16_t *passes) {
    *running = playing != NOTHING && !finished;
    *passes = passes_done;
}

static bool square_by_pwm = false;  // a pin without the square timer: PWM at 50 %

static void square_off() {
    if (square_pin == 0xFF) return;
    if (square_by_pwm) hal::pwm_stop(square_pin);
    else hal::square_stop();
    square_pin = 0xFF;
    square_by_pwm = false;
}

uint8_t square(uint8_t pin, float frequency, float *actual) {
    *actual = 0.0f;
    if (frequency == 0.0f) {
        if (square_pin != 0xFF) {
            uint8_t was = square_pin;
            square_off();
            hal::pin_output(was, false);
            app::driving(app::pin_bit(was));
        }
        return 0;
    }
    if (!(frequency > 0.0f)) return ERR_ARGUMENTS;
    if (pin >= board::PIN_COUNT) return ERR_ARGUMENTS;
    uint8_t error = (pin == square_pin) ? 0 : app::check_pins(app::pin_bit(pin), board::PC_DOUT);
    if (error) return error;
    uint16_t caps = board::pin(pin).caps;
    if (!(caps & (board::PC_SQUARE | board::PC_PWM))) return ERR_UNSUPPORTED;
    square_off();
    bool by_pwm = !(caps & board::PC_SQUARE);
    error = by_pwm ? hal::pwm_start(pin, frequency, 32768, actual) : hal::square_start(pin, frequency, actual);
    if (error) return error;
    square_pin = pin;
    square_by_pwm = by_pwm;
    app::released(app::pin_bit(pin));
    return 0;
}

uint8_t arb_load(const uint8_t *d, uint8_t n, uint16_t *loaded) {
#if FW_ARB_POINTS
    if (board::dac_pin() == board::NO_CHANNEL) return ERR_UNSUPPORTED;
    if (n < 2 || (n - 2) % 2) return ERR_ARGUMENTS;
    if (playing == WAVE) return ERR_BUSY;
    uint16_t offset = get_u16(d);
    uint16_t count = (uint16_t)((n - 2) / 2);
    if (offset > points_loaded) return ERR_ARGUMENTS;
    if ((uint32_t)offset + count > FW_ARB_POINTS) return ERR_OVERFLOW;
    uint16_t limit = (uint16_t)((1u << board::INFO.dac_bits) - 1);
    for (uint16_t i = 0; i < count; i++) {
        uint16_t value = get_u16(d + 2 + 2 * i);
        points[offset + i] = value > limit ? limit : value;
    }
    points_loaded = (uint16_t)(offset + count);
    *loaded = points_loaded;
    return 0;
#else
    (void)d;
    (void)n;
    (void)loaded;
    return ERR_UNSUPPORTED;
#endif
}

uint8_t arb_start(const uint8_t *d, uint8_t n, float *actual) {
#if FW_ARB_POINTS
    uint8_t dac = board::dac_pin();
    if (dac == board::NO_CHANNEL || !board::INFO.arb_max_rate) return ERR_UNSUPPORTED;
    if (n != 7) return ERR_ARGUMENTS;
    float rate = get_f32(d);
    uint16_t count = get_u16(d + 4);
    uint8_t passes = d[6];
    if (!(rate > 0.0f) || rate > (float)board::INFO.arb_max_rate || count == 0 || count > points_loaded)
        return ERR_ARGUMENTS;
    if (playing != NOTHING) return ERR_BUSY;
    uint8_t error = app::check_pins(app::pin_bit(dac), board::PC_DAC);
    if (error) return error;
    error = hal::dac_write(dac, points[0]);
    if (error) return error;
    wave_points = count;
    passes_wanted = passes;
    passes_done = 0;
    index_now = 0;
    finished = false;
    playing = WAVE;
    error = hal::timer_start(hal::TIMER_OUTPUT, rate, wave_tick, actual);
    if (error) {
        playing = NOTHING;
        return error;
    }
    return 0;
#else
    (void)d;
    (void)n;
    (void)actual;
    return ERR_UNSUPPORTED;
#endif
}

uint32_t pins() {
    uint32_t mask = 0;
    if (playing == PATTERN) mask |= pattern_mask;
    if (playing == WAVE) mask |= app::pin_bit(board::dac_pin());
    if (square_pin != 0xFF) mask |= app::pin_bit(square_pin);
    return mask;
}

bool driving() { return playing != NOTHING || square_pin != 0xFF; }

void stop_all() {
    uint32_t mask = pins();
    halt();
    square_off();
    if (board::dac_pin() != board::NO_CHANNEL && (mask & app::pin_bit(board::dac_pin()))) hal::dac_stop(board::dac_pin());
    for (uint8_t i = 0; i < board::PIN_COUNT; i++)
        if (mask & app::pin_bit(i)) hal::pin_input(i, hal::PULL_NONE);
}

void poll() {
    if (finished && playing != NOTHING) stop();
}

}  // namespace gen
