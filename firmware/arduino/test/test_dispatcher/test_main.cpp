// Copyright (C) 2026 Julian Decker
//
// Part of openSciLab.
//
// SPDX-License-Identifier: GPL-3.0-or-later

// The whole firmware on the fake board (src/arch/native): requests in as frames, answers and
// events out as frames. Captures, the generator and the watchdog run on the fake timers.

#include <unity.h>

#include <string>
#include <vector>

#include "app.h"
#include "arch/native/fake_hal.h"
#include "hal.h"
#include "protocol.h"

using namespace proto;
namespace fake = hal::fake;

struct Got {
    uint8_t type;
    uint8_t sequence;
    std::vector<uint8_t> payload;
    uint8_t command() const { return payload.empty() ? 0 : payload[0]; }
    std::string text() const { return std::string(payload.begin() + 1, payload.end()); }
    uint16_t u16(size_t at) const { return get_u16(&payload[at]); }
    uint32_t u32(size_t at) const { return get_u32(&payload[at]); }
    float f32(size_t at) const { return get_f32(&payload[at]); }
};

struct Data {
    std::vector<uint8_t> v;
    Data &u8(uint8_t x) { v.push_back(x); return *this; }
    Data &u16(uint16_t x) { u8((uint8_t)x); return u8((uint8_t)(x >> 8)); }
    Data &u32(uint32_t x) { u16((uint16_t)x); return u16((uint16_t)(x >> 16)); }
    Data &f32(float f) { uint32_t r; memcpy(&r, &f, 4); return u32(r); }
};

static uint8_t sequence = 0;
static Decoder line_decoder;

static std::vector<Got> drain() {
    std::vector<Got> frames;
    for (uint8_t byte : fake::line) {
        if (line_decoder.feed(byte)) {
            const Frame &f = line_decoder.frame();
            frames.push_back({f.type, f.sequence, std::vector<uint8_t>(f.payload, f.payload + f.length)});
        }
    }
    fake::line.clear();
    return frames;
}

static void to_app(const uint8_t *data, size_t n) {
    for (size_t i = 0; i < n; i++) app::receive(data[i]);
}

// one request: its answer (or error answer)
static Got request(uint8_t command, const Data &data = Data()) {
    Writer w;
    w.begin(T_REQUEST, ++sequence);
    w.u8(command);
    if (!data.v.empty()) w.bytes(data.v.data(), data.v.size());
    w.send(to_app);
    std::vector<Got> frames = drain();
    TEST_ASSERT_EQUAL_INT_MESSAGE(1, (int)frames.size(), "one answer per request");
    TEST_ASSERT_EQUAL_UINT8(sequence, frames[0].sequence);
    TEST_ASSERT_EQUAL_UINT8(command, frames[0].command());
    return frames[0];
}

static void ok(const Got &got) {
    if (got.type == T_ERROR) TEST_FAIL_MESSAGE(("error answer: " + got.text()).c_str());
    TEST_ASSERT_EQUAL_UINT8(T_ANSWER, got.type);
}

static void error(uint8_t code, const Got &got) {
    TEST_ASSERT_EQUAL_UINT8(T_ERROR, got.type);
    TEST_ASSERT_EQUAL_UINT8(code, got.payload[1]);
}

static std::vector<Got> poll(int times = 1) {
    for (int i = 0; i < times; i++) app::poll();
    return drain();
}

static std::vector<Got> events(const std::vector<Got> &frames, uint8_t code) {
    std::vector<Got> found;
    for (const Got &g : frames)
        if (g.type == T_EVENT && g.command() == code) found.push_back(g);
    return found;
}

static uint32_t bit(uint8_t n) { return (uint32_t)1 << n; }

void setUp() {
    fake::reset();
    line_decoder.reset();
    app::init();
    app::safe();
    fake::line.clear();
}

void tearDown() {
    request(CMD_CAPTURE_ABORT);
    poll(50);
    request(CMD_SAFE);
}

// ----------------------------------------------------------------------- information

void test_hello_answers_key_value_text() {
    Got got = request(CMD_HELLO);
    ok(got);
    TEST_ASSERT_EQUAL_STRING(
        "board=native;version=8.1.0;protocol=1;rate=1000000;clock=16000000;buffer=600;adc_bits=10;"
        "dac_bits=12;vref_mv=5000",
        got.text().c_str());
}

void test_caps_lists_what_the_board_can() {
    Got got = request(CMD_CAPS);
    ok(got);
    TEST_ASSERT_EQUAL_STRING(
        "GPIO,PWM,MONITOR,ANALOG=6,DAC,AFG,IMMEDIATE_TRIGGER,CONTINUOUS_STREAM,STREAM=11520,STATE_MODE,"
        "STREAM_STATE,PATTERN_GEN=100000,18,GEN_SQUARE,TX_UART,TX_SPI,TX_I2C",
        got.text().c_str());
}

void test_pins_answers_one_line_per_index_then_nothing() {
    TEST_ASSERT_EQUAL_STRING("PIN:D0,DIN,-,5000,-,reserved: USB serial (RX)",
                             request(CMD_PINS, Data().u8(0)).text().c_str());
    TEST_ASSERT_EQUAL_STRING("PIN:D3,DIN/DOUT/PULLUP/PWM/CLOCK,1,5000,-",
                             request(CMD_PINS, Data().u8(3)).text().c_str());
    TEST_ASSERT_EQUAL_STRING("PIN:A0,DIN/DOUT/PULLUP/ADC/DAC,12,5000,0",
                             request(CMD_PINS, Data().u8(14)).text().c_str());
    Got end = request(CMD_PINS, Data().u8(20));
    ok(end);
    TEST_ASSERT_EQUAL_size_t(1, end.payload.size());
}

void test_unknown_commands_and_bad_lengths_are_errors() {
    Got got = request(0x7E);
    error(ERR_UNKNOWN, got);
    TEST_ASSERT_EQUAL_STRING("unknown command", got.text().substr(1).c_str());
    error(ERR_ARGUMENTS, request(CMD_PIN_MODE, Data().u8(3)));
    error(ERR_ARGUMENTS, request(CMD_PIN_MODE, Data().u8(3).u8(9)));
    error(ERR_ARGUMENTS, request(CMD_PIN_MODE, Data().u8(40).u8(0)));
}

void test_events_and_answers_ignore_frames_that_are_no_requests() {
    Writer w;
    w.begin(T_ANSWER, 3);
    w.u8(CMD_HELLO);
    w.send(to_app);
    TEST_ASSERT_EQUAL_size_t(0, drain().size());
}

// ------------------------------------------------------------------------------- GPIO

void test_write_answers_as_the_fixture_and_drives_the_pins() {
    // the request and answer of tests/fixtures/arduino_frames.json (sequence 8, WRITE D13 high)
    const uint8_t request_wire[] = {0x04, 0x01, 0x08, 0x11, 0x02, 0x20, 0x01, 0x01, 0x02, 0x20, 0x01, 0x02, 0x48, 0x00};
    const uint8_t answer_wire[] = {0x05, 0x02, 0x08, 0x11, 0x09, 0x00};
    to_app(request_wire, sizeof(request_wire));
    TEST_ASSERT_EQUAL_size_t(sizeof(answer_wire), fake::line.size());
    TEST_ASSERT_EQUAL_MEMORY(answer_wire, fake::line.data(), sizeof(answer_wire));
    fake::line.clear();
    TEST_ASSERT_TRUE(fake::output_mask & bit(13));
    TEST_ASSERT_TRUE(fake::output_levels & bit(13));
    Got read = request(CMD_READ, Data().u32(bit(13) | bit(12)));
    ok(read);
    TEST_ASSERT_EQUAL_HEX32(bit(13), read.u32(1));
}

void test_reserved_and_unsuitable_pins_are_refused() {
    error(ERR_RESERVED, request(CMD_WRITE, Data().u32(bit(1)).u32(0)));
    error(ERR_RESERVED, request(CMD_PIN_MODE, Data().u8(0).u8(MODE_OUTPUT)));
    error(ERR_UNSUPPORTED, request(CMD_PIN_MODE, Data().u8(2).u8(MODE_INPUT_PULLDOWN)));
    ok(request(CMD_PIN_MODE, Data().u8(4).u8(MODE_INPUT_PULLDOWN)));
    TEST_ASSERT_TRUE(fake::pulldowns & bit(4));
    error(ERR_UNSUPPORTED, request(CMD_PWM, Data().u8(2).f32(1000).u16(100)));
    error(ERR_UNSUPPORTED, request(CMD_DAC, Data().u8(15).u16(100)));
    error(ERR_ARGUMENTS, request(CMD_READ, Data().u32(bit(25))));
}

void test_pwm_dac_and_pulse() {
    Got pwm = request(CMD_PWM, Data().u8(9).f32(1000.0f).u16(32768));
    ok(pwm);
    TEST_ASSERT_EQUAL_FLOAT(1000.0f, pwm.f32(1));
    TEST_ASSERT_TRUE(fake::pwm_pins & bit(9));
    ok(request(CMD_PWM, Data().u8(9).f32(1000.0f).u16(0)));  // duty 0: low
    TEST_ASSERT_FALSE(fake::pwm_pins & bit(9));
    TEST_ASSERT_FALSE(fake::output_levels & bit(9));

    ok(request(CMD_DAC, Data().u8(14).u16(4095)));
    TEST_ASSERT_EQUAL_UINT16(4095, fake::dac_value);
    error(ERR_ARGUMENTS, request(CMD_DAC, Data().u8(14).u16(4096)));

    uint32_t before = fake::now_us;
    ok(request(CMD_PULSE, Data().u8(7).u8(1).u32(250)));
    TEST_ASSERT_EQUAL_UINT32(before + 250, fake::now_us);
    TEST_ASSERT_FALSE(fake::output_levels & bit(7));  // back to the other level
    size_t n = fake::writes.size();
    TEST_ASSERT_TRUE(n >= 2);
    TEST_ASSERT_TRUE(fake::writes[n - 2] & bit(7));
}

void test_adc_read_answers_per_mask_bit() {
    fake::adc_values[0] = 100;
    fake::adc_values[2] = 1023;
    Got got = request(CMD_ADC_READ, Data().u16(0b101));
    ok(got);
    TEST_ASSERT_EQUAL_size_t(5, got.payload.size());
    TEST_ASSERT_EQUAL_UINT16(100, got.u16(1));
    TEST_ASSERT_EQUAL_UINT16(1023, got.u16(3));
    error(ERR_ARGUMENTS, request(CMD_ADC_READ, Data().u16(1 << 6)));
}

void test_monitor_reports_state_events() {
    fake::input_levels = bit(5);
    fake::adc_values[1] = 777;
    ok(request(CMD_MONITOR, Data().u32(100000).u32(bit(5) | bit(6)).u16(0b10)));  // 100 Hz
    std::vector<Got> states;
    for (int ms = 0; ms < 50; ms++) {
        fake::now_us += 1000;
        for (const Got &g : events(poll(), EVENT_STATE)) states.push_back(g);
    }
    TEST_ASSERT_TRUE(states.size() >= 4 && states.size() <= 6);
    TEST_ASSERT_EQUAL_size_t(11, states[0].payload.size());
    TEST_ASSERT_EQUAL_HEX32(bit(5), states[0].u32(5));
    TEST_ASSERT_EQUAL_UINT16(777, states[0].u16(9));
    ok(request(CMD_MONITOR, Data().u32(0).u32(0).u16(0)));
    fake::now_us += 100000;
    TEST_ASSERT_EQUAL_size_t(0, events(poll(), EVENT_STATE).size());
    error(ERR_ARGUMENTS, request(CMD_MONITOR, Data().u32(2000000).u32(1).u16(0)));  // above 1 kHz
}

// ------------------------------------------------------------------- watchdog, safe

void test_watchdog_releases_outputs_after_a_second_without_frames() {
    ok(request(CMD_WRITE, Data().u32(bit(8)).u32(bit(8))));
    fake::now_us += 600000;
    TEST_ASSERT_EQUAL_size_t(0, poll().size());
    ok(request(CMD_HEARTBEAT));  // keeps it satisfied
    fake::now_us += 600000;
    TEST_ASSERT_EQUAL_size_t(0, poll().size());
    TEST_ASSERT_TRUE(fake::output_mask & bit(8));
    fake::now_us += 500000;
    std::vector<Got> frames = poll();
    TEST_ASSERT_EQUAL_size_t(1, events(frames, EVENT_WATCHDOG).size());
    TEST_ASSERT_FALSE(fake::output_mask & bit(8));
    fake::now_us += 2000000;
    TEST_ASSERT_EQUAL_size_t(0, poll().size());  // once
}

void test_no_watchdog_without_outputs() {
    ok(request(CMD_PIN_MODE, Data().u8(8).u8(MODE_INPUT_PULLUP)));
    fake::now_us += 5000000;
    TEST_ASSERT_EQUAL_size_t(0, poll().size());
}

void test_safe_releases_everything() {
    ok(request(CMD_WRITE, Data().u32(bit(8)).u32(bit(8))));
    ok(request(CMD_PWM, Data().u8(3).f32(500.0f).u16(1000)));
    ok(request(CMD_SQUARE, Data().u8(9).f32(10000.0f)));
    ok(request(CMD_SAFE));
    TEST_ASSERT_EQUAL_HEX32(0, fake::output_mask);
    TEST_ASSERT_EQUAL_HEX32(0, fake::pwm_pins);
    TEST_ASSERT_EQUAL_INT(-1, fake::square_pin);
}

// --------------------------------------------------------------------------- captures

static Data setup_data(uint8_t mode, uint32_t rate, uint32_t mask, uint16_t adc, uint32_t samples, uint32_t pre,
                       uint8_t trigger = 0, uint32_t tmask = 0, uint32_t tlevels = 0, uint8_t clock = 0,
                       uint8_t edge = 0) {
    return Data().u8(mode).u32(rate).u32(mask).u16(adc).u32(samples).u32(pre).u8(trigger).u32(tmask).u32(tlevels)
        .u8(clock).u8(edge);
}

// the levels per sample from DATA kind 0 events (checks that the indexes follow each other)
static std::vector<uint32_t> samples_of(const std::vector<Got> &frames) {
    std::vector<uint32_t> levels;
    for (const Got &g : events(frames, EVENT_DATA)) {
        if (g.payload[1] != DATA_DIGITAL) continue;
        TEST_ASSERT_EQUAL_UINT32(levels.size(), g.u32(2));
        for (size_t at = 6; at + 6 <= g.payload.size(); at += 6)
            levels.insert(levels.end(), g.u16(at), g.u32(at + 2));
    }
    return levels;
}

static std::vector<Got> until_done(int limit = 500) {
    std::vector<Got> all;
    for (int i = 0; i < limit; i++) {
        std::vector<Got> got = poll();
        all.insert(all.end(), got.begin(), got.end());
        if (!events(got, EVENT_DONE).empty()) break;
    }
    return all;
}

void test_stream_capture_sends_runs_and_done() {
    Got setup = request(CMD_CAPTURE_SETUP, setup_data(MODE_STREAM, 1000, bit(2) | bit(3), 0, 0, 0));
    ok(setup);
    TEST_ASSERT_EQUAL_UINT32(1000, setup.u32(1));
    ok(request(CMD_CAPTURE_START));
    std::vector<uint32_t> expected;
    const uint32_t pattern[] = {0, 0, 0, bit(2), bit(2), bit(2) | bit(3), 0, 0, 0, 0, bit(3)};
    std::vector<Got> all;
    for (uint32_t levels : pattern) {
        fake::input_levels = levels | bit(7);  // D7 is not captured
        fake::tick(hal::TIMER_SAMPLE, 1);
        expected.push_back(levels);
        std::vector<Got> got = poll();
        all.insert(all.end(), got.begin(), got.end());
    }
    error(ERR_BUSY, request(CMD_WRITE, Data().u32(bit(2)).u32(0)));  // a capture pin
    error(ERR_BUSY, request(CMD_CAPTURE_SETUP, setup_data(MODE_STREAM, 1000, bit(2), 0, 0, 0)));
    ok(request(CMD_CAPTURE_ABORT));
    std::vector<Got> rest = until_done();
    all.insert(all.end(), rest.begin(), rest.end());
    TEST_ASSERT_EQUAL_UINT32_ARRAY(expected.data(), samples_of(all).data(), expected.size());
    std::vector<Got> done = events(all, EVENT_DONE);
    TEST_ASSERT_EQUAL_size_t(1, done.size());
    TEST_ASSERT_EQUAL_UINT8(DONE_STOPPED, done[0].payload[1]);
    TEST_ASSERT_EQUAL_UINT32(expected.size(), done[0].u32(2));
}

void test_stream_capture_with_samples_and_trigger_completes() {
    ok(request(CMD_CAPTURE_SETUP, setup_data(MODE_STREAM, 2000, bit(4), 0, 5, 0, TRIGGER_RISING, bit(6), 0)));
    ok(request(CMD_CAPTURE_START));
    fake::tick(hal::TIMER_SAMPLE, 3);  // no trigger yet
    fake::input_levels = bit(6) | bit(4);
    fake::tick(hal::TIMER_SAMPLE, 2);
    fake::input_levels = bit(6);
    fake::tick(hal::TIMER_SAMPLE, 10);
    std::vector<Got> all = until_done();
    std::vector<uint32_t> levels = samples_of(all);
    const uint32_t expected[] = {bit(4), bit(4), 0, 0, 0};
    TEST_ASSERT_EQUAL_size_t(5, levels.size());
    TEST_ASSERT_EQUAL_UINT32_ARRAY(expected, levels.data(), 5);
    std::vector<Got> done = events(all, EVENT_DONE);
    TEST_ASSERT_EQUAL_UINT8(DONE_COMPLETE, done[0].payload[1]);
    TEST_ASSERT_EQUAL_UINT32(5, done[0].u32(2));
}

void test_stream_capture_reports_overflow() {
    ok(request(CMD_CAPTURE_SETUP, setup_data(MODE_STREAM, 1000, bit(2), 0, 0, 0)));
    ok(request(CMD_CAPTURE_START));
    for (int i = 0; i < 300; i++) {  // a change per sample and nobody empties the ring
        fake::input_levels = (i & 1) ? bit(2) : 0;
        fake::tick(hal::TIMER_SAMPLE, 1);
    }
    std::vector<Got> all = until_done();
    std::vector<Got> done = events(all, EVENT_DONE);
    TEST_ASSERT_EQUAL_size_t(1, done.size());
    TEST_ASSERT_EQUAL_UINT8(DONE_OVERFLOW, done[0].payload[1]);
    TEST_ASSERT_EQUAL_UINT32(samples_of(all).size(), done[0].u32(2));
    TEST_ASSERT_TRUE(done[0].u32(2) > 50 && done[0].u32(2) < 300);
}

void test_stream_capture_with_analog_channels() {
    fake::adc_values[0] = 11;
    fake::adc_values[3] = 33;
    ok(request(CMD_CAPTURE_SETUP, setup_data(MODE_STREAM, 1000, 0, 0b1001, 4, 0)));
    ok(request(CMD_CAPTURE_START));
    poll(4);  // the main loop converts the channels in turn
    fake::tick(hal::TIMER_SAMPLE, 4);
    std::vector<Got> all = until_done();
    std::vector<uint16_t> values;
    for (const Got &g : events(all, EVENT_DATA)) {
        TEST_ASSERT_EQUAL_UINT8(DATA_ANALOG, g.payload[1]);
        for (size_t at = 6; at + 2 <= g.payload.size(); at += 2) values.push_back(g.u16(at));
    }
    const uint16_t expected[] = {11, 33, 11, 33, 11, 33, 11, 33};
    TEST_ASSERT_EQUAL_size_t(8, values.size());
    TEST_ASSERT_EQUAL_UINT16_ARRAY(expected, values.data(), 8);
}

void test_state_capture_takes_a_frame_per_clock_edge_and_counts_losses() {
    error(ERR_UNSUPPORTED, request(CMD_CAPTURE_SETUP, setup_data(MODE_STATE, 0, bit(4), 0, 0, 0, 0, 0, 0, 14)));
    ok(request(CMD_CAPTURE_SETUP, setup_data(MODE_STATE, 0, bit(4) | bit(5), 0, 0, 0, 0, 0, 0, 2, 1)));
    ok(request(CMD_CAPTURE_START));
    TEST_ASSERT_EQUAL_UINT8(2, fake::edge_pin);
    TEST_ASSERT_TRUE(fake::edge_falling);
    for (int i = 0; i < 3; i++) {
        fake::now_us += 100;
        fake::input_levels = (uint32_t)i << 4;
        fake::edge();
    }
    std::vector<Got> all = poll(3);
    for (int i = 0; i < 80; i++) fake::edge();  // the ring holds 74 frames
    ok(request(CMD_CAPTURE_ABORT));
    std::vector<Got> rest = until_done();
    all.insert(all.end(), rest.begin(), rest.end());
    std::vector<uint32_t> times, levels;
    for (const Got &g : events(all, EVENT_DATA)) {
        TEST_ASSERT_EQUAL_UINT8(DATA_STATE, g.payload[1]);
        TEST_ASSERT_EQUAL_UINT32(times.size(), g.u32(2));
        for (size_t at = 6; at + 8 <= g.payload.size(); at += 8) {
            times.push_back(g.u32(at));
            levels.push_back(g.u32(at + 4));
        }
    }
    TEST_ASSERT_EQUAL_UINT32(1100, times[0]);
    TEST_ASSERT_EQUAL_UINT32(1300, times[2]);
    TEST_ASSERT_EQUAL_HEX32(bit(5), levels[2]);
    std::vector<Got> done = events(all, EVENT_DONE);
    TEST_ASSERT_EQUAL_UINT8(DONE_STOPPED, done[0].payload[1]);
    TEST_ASSERT_EQUAL_UINT32(times.size(), done[0].u32(2));
    TEST_ASSERT_EQUAL_UINT32(3 + 80 - times.size(), done[0].u32(6));  // edges lost
    TEST_ASSERT_TRUE(done[0].u32(6) > 0);
}

static uint32_t buffer_input() {
    uint32_t sample = fake::cycle_counter / 16000;  // 1 kHz on the 16 MHz counted clock
    return ((sample & 1) ? bit(2) : 0) | (sample >= 50 ? bit(3) : 0);
}

void test_buffer_capture_with_pre_trigger() {
    error(ERR_OVERFLOW, request(CMD_CAPTURE_SETUP, setup_data(MODE_BUFFER, 1000, bit(2) | bit(3), 0, 601, 0)));
    error(ERR_UNSUPPORTED, request(CMD_CAPTURE_SETUP, setup_data(MODE_BUFFER, 1000, bit(2), 1, 100, 0)));
    Got setup = request(CMD_CAPTURE_SETUP,
                        setup_data(MODE_BUFFER, 1000, bit(2) | bit(3), 0, 100, 10, TRIGGER_RISING, bit(3), 0));
    ok(setup);
    TEST_ASSERT_EQUAL_UINT32(1000, setup.u32(1));
    fake::input_source = buffer_input;
    ok(request(CMD_CAPTURE_START));
    std::vector<Got> all = until_done();
    std::vector<uint32_t> levels = samples_of(all);
    TEST_ASSERT_EQUAL_size_t(100, levels.size());
    for (size_t i = 0; i < 10; i++) TEST_ASSERT_FALSE(levels[i] & bit(3));
    for (size_t i = 10; i < 100; i++) TEST_ASSERT_TRUE(levels[i] & bit(3));
    for (size_t i = 1; i < 100; i++) TEST_ASSERT_NOT_EQUAL(levels[i - 1] & bit(2), levels[i] & bit(2));
    std::vector<Got> done = events(all, EVENT_DONE);
    TEST_ASSERT_EQUAL_UINT8(DONE_COMPLETE, done[0].payload[1]);
    TEST_ASSERT_EQUAL_UINT32(100, done[0].u32(2));
    TEST_ASSERT_FALSE(fake::clock_busy);
}

void test_buffer_capture_waiting_for_its_trigger_locks_gpio_and_aborts() {
    ok(request(CMD_CAPTURE_SETUP,
               setup_data(MODE_BUFFER, 1000, bit(2), 0, 50, 0, TRIGGER_PATTERN, bit(4), bit(4))));
    ok(request(CMD_CAPTURE_START));
    TEST_ASSERT_EQUAL_size_t(0, poll(2).size());  // waits
    error(ERR_BUSY, request(CMD_WRITE, Data().u32(bit(8)).u32(0)));  // AVR-like: GPIO waits
    ok(request(CMD_HEARTBEAT));
    ok(request(CMD_CAPTURE_ABORT));
    std::vector<Got> done = events(until_done(), EVENT_DONE);
    TEST_ASSERT_EQUAL_size_t(1, done.size());
    TEST_ASSERT_EQUAL_UINT8(DONE_STOPPED, done[0].payload[1]);
    TEST_ASSERT_EQUAL_UINT32(0, done[0].u32(2));
    ok(request(CMD_WRITE, Data().u32(bit(8)).u32(0)));
}

// ------------------------------------------------------------------------- generator

static Data runs_data(uint16_t offset, std::vector<std::pair<uint16_t, uint32_t>> runs) {
    Data d;
    d.u16(offset);
    for (auto &r : runs) d.u16(r.first).u32(r.second);
    return d;
}

void test_pattern_generator_plays_runs() {
    Got load = request(CMD_GEN_LOAD, runs_data(0, {{2, bit(4)}, {1, bit(5)}}));
    ok(load);
    TEST_ASSERT_EQUAL_UINT16(2, load.u16(1));
    load = request(CMD_GEN_LOAD, runs_data(2, {{3, 0}}));
    TEST_ASSERT_EQUAL_UINT16(3, load.u16(1));
    error(ERR_ARGUMENTS, request(CMD_GEN_LOAD, runs_data(9, {{1, 0}})));  // a gap
    error(ERR_ARGUMENTS, request(CMD_GEN_LOAD, runs_data(3, {{0, 0}})));  // a run of 0 samples
    Got start = request(CMD_GEN_START, Data().f32(1000.0f).u32(bit(4) | bit(5)).u16(2).u8(0));
    ok(start);
    TEST_ASSERT_EQUAL_FLOAT(1000.0f, start.f32(1));
    error(ERR_BUSY, request(CMD_WRITE, Data().u32(bit(4)).u32(0)));
    error(ERR_BUSY, request(CMD_GEN_LOAD, runs_data(0, {{1, 0}})));
    // levels per sample: 2 x D4, 1 x D5, 3 x none; two passes
    const uint32_t expected[] = {bit(4), bit(4), bit(5), 0, 0, 0, bit(4), bit(4), bit(5), 0, 0, 0};
    std::vector<uint32_t> levels;
    for (int i = 0; i < 12; i++) {
        levels.push_back(fake::output_levels & (bit(4) | bit(5)));
        fake::tick(hal::TIMER_OUTPUT, 1);
    }
    TEST_ASSERT_EQUAL_UINT32_ARRAY(expected, levels.data(), 12);
    Got status = request(CMD_GEN_STATUS);
    TEST_ASSERT_EQUAL_UINT8(0, status.payload[1]);  // over after two passes
    TEST_ASSERT_EQUAL_UINT16(2, status.u16(2));
    poll();
    TEST_ASSERT_NULL(fake::timer_handler[hal::TIMER_OUTPUT]);
    ok(request(CMD_WRITE, Data().u32(bit(4)).u32(0)));
}

void test_pattern_generator_limits() {
    std::vector<std::pair<uint16_t, uint32_t>> many(17, {1, 0});
    error(ERR_OVERFLOW, request(CMD_GEN_LOAD, runs_data(0, many)));
    ok(request(CMD_GEN_LOAD, runs_data(0, {{1, 0}, {1, bit(4)}})));
    error(ERR_UNSUPPORTED, request(CMD_GEN_START, Data().f32(1000.0f).u32(bit(4)).u16(0).u8(1)));
    error(ERR_ARGUMENTS, request(CMD_GEN_START, Data().f32(200000.0f).u32(bit(4)).u16(0).u8(0)));
    error(ERR_RESERVED, request(CMD_GEN_START, Data().f32(1000.0f).u32(bit(1)).u16(0).u8(0)));
    ok(request(CMD_GEN_START, Data().f32(1000.0f).u32(bit(4)).u16(0).u8(0)));
    fake::tick(hal::TIMER_OUTPUT, 7);
    Got status = request(CMD_GEN_STATUS);
    TEST_ASSERT_EQUAL_UINT8(1, status.payload[1]);
    TEST_ASSERT_EQUAL_UINT16(3, status.u16(2));
    // a generator counts as driven outputs for the watchdog
    fake::now_us += 1500000;
    TEST_ASSERT_EQUAL_size_t(1, events(poll(), EVENT_WATCHDOG).size());
    TEST_ASSERT_NULL(fake::timer_handler[hal::TIMER_OUTPUT]);
}

void test_square_and_arbitrary_waveform() {
    Got square = request(CMD_SQUARE, Data().u8(9).f32(12345.0f));
    ok(square);
    TEST_ASSERT_EQUAL_FLOAT(12345.0f, square.f32(1));
    TEST_ASSERT_EQUAL_INT(9, fake::square_pin);
    error(ERR_UNSUPPORTED, request(CMD_SQUARE, Data().u8(2).f32(100.0f)));  // neither timer nor PWM
    ok(request(CMD_SQUARE, Data().u8(5).f32(100.0f)));                      // PWM at 50 %
    TEST_ASSERT_EQUAL_INT(-1, fake::square_pin);
    TEST_ASSERT_TRUE(fake::pwm_pins & bit(5));
    TEST_ASSERT_EQUAL_UINT16(32768, fake::pwm_duty[5]);
    ok(request(CMD_SQUARE, Data().u8(9).f32(12345.0f)));
    TEST_ASSERT_FALSE(fake::pwm_pins & bit(5));
    ok(request(CMD_SQUARE, Data().u8(9).f32(0.0f)));
    TEST_ASSERT_EQUAL_INT(-1, fake::square_pin);

    Got load = request(CMD_ARB_LOAD, Data().u16(0).u16(0).u16(1000).u16(4095).u16(2000));
    ok(load);
    TEST_ASSERT_EQUAL_UINT16(4, load.u16(1));
    ok(request(CMD_ARB_START, Data().f32(100.0f).u16(4).u8(1)));
    std::vector<uint16_t> values;
    for (int i = 0; i < 4; i++) {
        values.push_back(fake::dac_value);
        fake::tick(hal::TIMER_OUTPUT, 1);
    }
    const uint16_t expected[] = {0, 1000, 4095, 2000};
    TEST_ASSERT_EQUAL_UINT16_ARRAY(expected, values.data(), 4);
    TEST_ASSERT_EQUAL_UINT8(0, request(CMD_GEN_STATUS).payload[1]);
}

// ----------------------------------------------------------------------- sending

void test_tx_uart_sends_start_data_stop_bits() {
    fake::output_mask = 0;
    Data d;
    d.u8(6).u32(9600).u8(0xA5);
    ok(request(CMD_TX_UART, d));
    // after setting the pin high (idle): start, 8 data bits LSB first, stop
    std::vector<uint32_t> &w = fake::writes;
    TEST_ASSERT_TRUE(w.size() >= 10);
    std::vector<int> bits;
    for (size_t i = w.size() - 10; i < w.size(); i++) bits.push_back((w[i] & bit(6)) ? 1 : 0);
    const int expected[] = {0, 1, 0, 1, 0, 0, 1, 0, 1, 1};
    TEST_ASSERT_EQUAL_INT_ARRAY(expected, bits.data(), 10);
    TEST_ASSERT_TRUE(fake::output_levels & bit(6));
    TEST_ASSERT_FALSE(fake::clock_busy);
}

void test_tx_spi_answers_the_bytes_read() {
    fake::input_levels = bit(12);  // MISO high
    Data d;
    d.u8(13).u8(11).u8(12).u8(10).u32(100000).u8(0x12).u8(0x34);
    Got got = request(CMD_TX_SPI, d);
    ok(got);
    TEST_ASSERT_EQUAL_size_t(3, got.payload.size());
    TEST_ASSERT_EQUAL_UINT8(0xFF, got.payload[1]);
    TEST_ASSERT_TRUE(fake::output_levels & bit(10));  // CS high again
    Data without;
    without.u8(13).u8(11).u8(0xFF).u8(0xFF).u32(100000).u8(0x12);
    got = request(CMD_TX_SPI, without);
    ok(got);
    TEST_ASSERT_EQUAL_size_t(1, got.payload.size());
    error(ERR_RESERVED, request(CMD_TX_SPI, Data().u8(1).u8(11).u8(0xFF).u8(0xFF).u32(1000).u8(1)));
}

void test_tx_i2c_without_a_device_is_not_acknowledged() {
    Data d;
    d.u8(18).u8(19).u8(0x48).u32(100000).u8(2).u8(0x00);
    error(ERR_NACK, request(CMD_TX_I2C, d));
    error(ERR_ARGUMENTS, request(CMD_TX_I2C, Data().u8(18).u8(19).u8(0x80).u32(100000).u8(0)));
    TEST_ASSERT_FALSE(fake::output_mask & (bit(18) | bit(19)));  // the lines are released
}

int main() {
    UNITY_BEGIN();
    RUN_TEST(test_hello_answers_key_value_text);
    RUN_TEST(test_caps_lists_what_the_board_can);
    RUN_TEST(test_pins_answers_one_line_per_index_then_nothing);
    RUN_TEST(test_unknown_commands_and_bad_lengths_are_errors);
    RUN_TEST(test_events_and_answers_ignore_frames_that_are_no_requests);
    RUN_TEST(test_write_answers_as_the_fixture_and_drives_the_pins);
    RUN_TEST(test_reserved_and_unsuitable_pins_are_refused);
    RUN_TEST(test_pwm_dac_and_pulse);
    RUN_TEST(test_adc_read_answers_per_mask_bit);
    RUN_TEST(test_monitor_reports_state_events);
    RUN_TEST(test_watchdog_releases_outputs_after_a_second_without_frames);
    RUN_TEST(test_no_watchdog_without_outputs);
    RUN_TEST(test_safe_releases_everything);
    RUN_TEST(test_stream_capture_sends_runs_and_done);
    RUN_TEST(test_stream_capture_with_samples_and_trigger_completes);
    RUN_TEST(test_stream_capture_reports_overflow);
    RUN_TEST(test_stream_capture_with_analog_channels);
    RUN_TEST(test_state_capture_takes_a_frame_per_clock_edge_and_counts_losses);
    RUN_TEST(test_buffer_capture_with_pre_trigger);
    RUN_TEST(test_buffer_capture_waiting_for_its_trigger_locks_gpio_and_aborts);
    RUN_TEST(test_pattern_generator_plays_runs);
    RUN_TEST(test_pattern_generator_limits);
    RUN_TEST(test_square_and_arbitrary_waveform);
    RUN_TEST(test_tx_uart_sends_start_data_stop_bits);
    RUN_TEST(test_tx_spi_answers_the_bytes_read);
    RUN_TEST(test_tx_i2c_without_a_device_is_not_acknowledged);
    return UNITY_END();
}
