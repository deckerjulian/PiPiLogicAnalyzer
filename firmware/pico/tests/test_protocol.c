/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

// Host tests of the frame parser (proto.c), the pattern data (pattern_data.c) and the dividers
// and report lines (timing.c) of the Pico firmware.

#include <stdio.h>
#include <string.h>
#include <math.h>
#include "proto.h"
#include "pattern_data.h"
#include "timing.h"

static int failures = 0;
static int checks = 0;

#define CHECK(condition) do { checks++; if(!(condition)) { failures++; printf("%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #condition); } } while(0)

// ------------------------------------------------------------------ frames

// Feeds bytes, returns the number of frames; the last payload is copied to out
static int feed(PROTO_PARSER* parser, const uint8_t* bytes, size_t count, uint8_t* out, uint16_t* outLength, int* cancels, int* overflows)
{
    int frames = 0;

    for(size_t i = 0; i < count; i++)
    {
        const uint8_t* payload;
        uint16_t length;
        PROTO_EVENT event = proto_feed(parser, bytes[i], &payload, &length);

        if(event == PROTO_FRAME)
        {
            frames++;
            memcpy(out, payload, length);
            *outLength = length;
        }
        else if(event == PROTO_CANCEL && cancels)
            (*cancels)++;
        else if(event == PROTO_OVERFLOW && overflows)
            (*overflows)++;
    }

    return frames;
}

static void test_frames(void)
{
    PROTO_PARSER parser;
    uint8_t payload[256];
    uint8_t frame[PROTO_FRAME_MAX];
    uint8_t out[PROTO_FRAME_MAX];
    uint16_t outLength = 0;

    // Every byte value, the frame bytes and the escape byte among them
    for(int i = 0; i < 200; i++)
        payload[i] = (uint8_t)(i * 37 + 0x55);
    payload[0] = CMD_GEN_LOAD;
    payload[1] = 0xAA;
    payload[2] = 0x55;
    payload[3] = 0xF0;
    payload[4] = 0xFF;

    uint16_t size = proto_build(payload, 200, frame, sizeof(frame));
    CHECK(size > 204);
    CHECK(frame[0] == 0x55 && frame[1] == 0xAA && frame[size - 2] == 0xAA && frame[size - 1] == 0x55);

    for(uint16_t i = 2; i < size - 2; i++)
        CHECK(frame[i] != 0xAA && frame[i] != 0x55);

    proto_reset(&parser);
    CHECK(feed(&parser, frame, size, out, &outLength, NULL, NULL) == 1);
    CHECK(outLength == 200);
    CHECK(memcmp(out, payload, 200) == 0);

    // Noise before the frame, a repeated start byte, two frames in a row
    {
        uint8_t bytes[64];
        size_t count = 0;
        uint8_t one[] = { CMD_READ, 0x01, 0x02, 0x03, 0x04 };
        uint8_t two[] = { CMD_HEARTBEAT };
        bytes[count++] = 0x12;
        bytes[count++] = 0x55;
        bytes[count++] = 0x13;
        bytes[count++] = 0x55;
        count += proto_build(one, sizeof(one), bytes + count, sizeof(bytes) - count);
        count += proto_build(two, sizeof(two), bytes + count, sizeof(bytes) - count);

        int cancels = 0;
        proto_reset(&parser);
        CHECK(feed(&parser, bytes, count, out, &outLength, &cancels, NULL) == 2);
        CHECK(outLength == 1 && out[0] == CMD_HEARTBEAT);
        CHECK(cancels == 0);
    }

    // 0xFF outside a frame cancels, inside a frame it is data
    {
        uint8_t data[] = { CMD_WRITE, 0xFF, 0xFF, 0xFF, 0xFF, 0x00, 0x00, 0x00, 0x00 };
        uint8_t bytes[64];
        size_t count = 0;
        bytes[count++] = 0xFF;
        count += proto_build(data, sizeof(data), bytes + count, sizeof(bytes) - count);
        bytes[count++] = 0xFF;

        int cancels = 0;
        proto_reset(&parser);
        CHECK(feed(&parser, bytes, count, out, &outLength, &cancels, NULL) == 1);
        CHECK(cancels == 2);
        CHECK(outLength == sizeof(data) && memcmp(out, data, sizeof(data)) == 0);
    }

    // 0x55 then 0xFF: no frame started, the 0xFF still cancels
    {
        uint8_t bytes[] = { 0x55, 0xFF };
        int cancels = 0;
        proto_reset(&parser);
        CHECK(feed(&parser, bytes, sizeof(bytes), out, &outLength, &cancels, NULL) == 0);
        CHECK(cancels == 1);
    }

    // An empty frame
    {
        uint8_t bytes[] = { 0x55, 0xAA, 0xAA, 0x55 };
        proto_reset(&parser);
        outLength = 99;
        CHECK(feed(&parser, bytes, sizeof(bytes), out, &outLength, NULL, NULL) == 1);
        CHECK(outLength == 0);
    }

    // Too long: dropped, the next frame is read again
    {
        static uint8_t bytes[PROTO_FRAME_MAX + 64];
        size_t count = 0;
        bytes[count++] = 0x55;
        bytes[count++] = 0xAA;
        while(count < PROTO_FRAME_MAX + 10)
            bytes[count++] = 0x11;
        uint8_t data[] = { CMD_SAFE };
        count += proto_build(data, sizeof(data), bytes + count, sizeof(bytes) - count);

        int overflows = 0;
        proto_reset(&parser);
        CHECK(feed(&parser, bytes, count, out, &outLength, NULL, &overflows) == 1);
        CHECK(overflows == 1);
        CHECK(outLength == 1 && out[0] == CMD_SAFE);
    }

    // The longest request: a trigger sequence of 8 stages with every byte escaped fits
    {
        uint8_t data[1 + 4 + 8 * 28];
        memset(data, 0xAA, sizeof(data));
        size = proto_build(data, sizeof(data), frame, sizeof(frame));
        CHECK(size == 2 + 2 * sizeof(data) + 2);
        proto_reset(&parser);
        CHECK(feed(&parser, frame, size, out, &outLength, NULL, NULL) == 1);
        CHECK(outLength == sizeof(data));
    }

    // Data of 200 bytes behind a header, every byte escaped: still one frame
    {
        uint8_t data[1 + 12 + PROTO_DATA_MAX];
        memset(data, 0xF0, sizeof(data));
        CHECK(proto_build(data, sizeof(data), frame, sizeof(frame)) == 2 + 2 * sizeof(data) + 2);
    }

    // Little endian fields
    {
        uint8_t data[] = { 0x78, 0x56, 0x34, 0x12, 0x00, 0x00, 0x80, 0x3F };
        CHECK(proto_u16(data) == 0x5678);
        CHECK(proto_u32(data) == 0x12345678);
        CHECK(proto_f32(data + 4) == 1.0f);
    }
}

static void test_allowed_while_capturing(void)
{
    for(int command = 0; command < 256; command++)
    {
        bool expected = (command >= 13 && command <= 20) || command == 23 || command == 24;
        CHECK(proto_allowed_while_capturing((uint8_t)command) == expected);
    }
}

// ---------------------------------------------------------- pattern data

static void test_pattern_data(void)
{
    uint8_t buffer[64];
    uint32_t end = 0;

    CHECK(gen_data_width(0) == 1);
    CHECK(gen_data_width(1 << GEN_LOAD_WIDTH_SHIFT) == 2);
    CHECK(gen_data_width(2 << GEN_LOAD_WIDTH_SHIFT) == 4);
    CHECK(gen_data_width(3 << GEN_LOAD_WIDTH_SHIFT) == 0);
    CHECK(gen_data_width_for_pins(1) == 1 && gen_data_width_for_pins(8) == 1);
    CHECK(gen_data_width_for_pins(9) == 2 && gen_data_width_for_pins(16) == 2);
    CHECK(gen_data_width_for_pins(17) == 4 && gen_data_width_for_pins(24) == 4);

    // Raw bytes at an offset
    {
        uint8_t data[] = { 1, 2, 3 };
        memset(buffer, 0xEE, sizeof(buffer));
        CHECK(gen_data_load(buffer, sizeof(buffer), 4, 0, data, sizeof(data), &end) == GEN_DATA_OK);
        CHECK(end == 7);
        CHECK(buffer[3] == 0xEE && buffer[4] == 1 && buffer[6] == 3 && buffer[7] == 0xEE);
    }

    // Run lengths of 1 byte samples, as protocol.pattern_blocks packs them
    {
        uint8_t data[] = { 3, 0, 0xA5, 1, 0, 0x5A, 2, 0, 0x00 };
        memset(buffer, 0xEE, sizeof(buffer));
        CHECK(gen_data_load(buffer, sizeof(buffer), 0, GEN_LOAD_RUNS, data, sizeof(data), &end) == GEN_DATA_OK);
        CHECK(end == 6);
        uint8_t expected[] = { 0xA5, 0xA5, 0xA5, 0x5A, 0x00, 0x00, 0xEE };
        CHECK(memcmp(buffer, expected, sizeof(expected)) == 0);
    }

    // Run lengths of 2 and 4 byte samples (little endian)
    {
        uint8_t data[] = { 2, 0, 0x34, 0x12, 1, 0, 0xCD, 0xAB };
        memset(buffer, 0xEE, sizeof(buffer));
        CHECK(gen_data_load(buffer, sizeof(buffer), 1, GEN_LOAD_RUNS | (1 << GEN_LOAD_WIDTH_SHIFT), data, sizeof(data), &end) == GEN_DATA_OK);
        CHECK(end == 4);
        uint8_t expected[] = { 0xEE, 0xEE, 0x34, 0x12, 0x34, 0x12, 0xCD, 0xAB, 0xEE };
        CHECK(memcmp(buffer, expected, sizeof(expected)) == 0);

        uint8_t wide[] = { 2, 0, 0x01, 0x02, 0x03, 0x00 };
        memset(buffer, 0xEE, sizeof(buffer));
        CHECK(gen_data_load(buffer, sizeof(buffer), 0, GEN_LOAD_RUNS | (2 << GEN_LOAD_WIDTH_SHIFT), wide, sizeof(wide), &end) == GEN_DATA_OK);
        CHECK(end == 2);
        uint8_t expectedWide[] = { 1, 2, 3, 0, 1, 2, 3, 0, 0xEE };
        CHECK(memcmp(buffer, expectedWide, sizeof(expectedWide)) == 0);
    }

    // A long run (the 16 bit count) fills the buffer exactly; one sample more is full
    {
        static uint8_t big[70000];
        uint8_t data[] = { 0xFF, 0xFF, 0x77, 0x01, 0x00, 0x66 };
        CHECK(gen_data_load(big, 65536, 0, GEN_LOAD_RUNS, data, sizeof(data), &end) == GEN_DATA_OK);
        CHECK(end == 65536 && big[0] == 0x77 && big[65534] == 0x77 && big[65535] == 0x66);

        memset(big, 0, sizeof(big));
        CHECK(gen_data_load(big, 65536, 1, GEN_LOAD_RUNS, data, sizeof(data), &end) == GEN_DATA_FULL);
        CHECK(big[1] == 0 && big[65535] == 0); // nothing written
    }

    // Errors
    {
        uint8_t data[] = { 0, 0, 0x11, 1, 0, 0x22 };
        CHECK(gen_data_load(buffer, sizeof(buffer), 0, GEN_LOAD_RUNS, data, sizeof(data), &end) == GEN_DATA_FORMAT); // run of 0
        CHECK(gen_data_load(buffer, sizeof(buffer), 0, GEN_LOAD_RUNS, data, 5, &end) == GEN_DATA_FORMAT); // half a pair
        CHECK(gen_data_load(buffer, sizeof(buffer), 0, 1 << GEN_LOAD_WIDTH_SHIFT, data, 3, &end) == GEN_DATA_FORMAT); // half a sample
        CHECK(gen_data_load(buffer, sizeof(buffer), 0, 3 << GEN_LOAD_WIDTH_SHIFT, data, 4, &end) == GEN_DATA_FORMAT); // width code
        CHECK(gen_data_load(buffer, sizeof(buffer), 0, 0x80, data, 4, &end) == GEN_DATA_FORMAT); // unknown flag
        CHECK(gen_data_load(buffer, sizeof(buffer), 63, 0, data, 2, &end) == GEN_DATA_FULL);
        CHECK(gen_data_load(buffer, sizeof(buffer), 0xFFFFFFFFu, 0, data, 2, &end) == GEN_DATA_FULL);
    }

    // Nothing at offset 0: an empty buffer
    CHECK(gen_data_load(buffer, sizeof(buffer), 0, GEN_LOAD_RUNS, NULL, 0, &end) == GEN_DATA_OK && end == 0);
}

// --------------------------------------------------------------- dividers

static void test_pwm(void)
{
    PWM_TIMING timing;

    CHECK(timing_pwm(200000000, 1000.0f, &timing));
    CHECK(fabs(timing.actual - 1000.0) < 0.1);
    CHECK(timing.top < 65535 && timing.div16 >= 16 && timing.div16 <= 4095);

    CHECK(timing_pwm(200000000, 1000000.0f, &timing));
    CHECK(timing.div16 == 16 && timing.top == 199 && fabs(timing.actual - 1e6) < 1e-6);

    CHECK(timing_pwm(200000000, 100000000.0f, &timing)); // half the clock: top 1
    CHECK(timing.top == 1);
    CHECK(!timing_pwm(200000000, 150000000.0f, &timing));
    CHECK(!timing_pwm(200000000, 1.0f, &timing)); // below the divider range
    CHECK(timing_pwm(200000000, 15.0f, &timing));
    CHECK(fabs(timing.actual - 15.0) / 15.0 < 0.001);
    CHECK(!timing_pwm(200000000, 0.0f, &timing));
    CHECK(!timing_pwm(200000000, -5.0f, &timing));
    CHECK(!timing_pwm(200000000, NAN, &timing));

    CHECK(timing_pwm_level(0, 199) == 0);
    CHECK(timing_pwm_level(65535, 199) == 200); // always high
    CHECK(timing_pwm_level(32768, 199) == 100);
    CHECK(timing_pwm_level(65535, 65534) == 65535);
}

static void test_pulse(void)
{
    uint32_t high, low;

    CHECK(timing_ns_to_cycles(200000000, 1000) == 200);
    CHECK(timing_ns_to_cycles(200000000, 12) == 2); // 2.4 cycles
    CHECK(timing_ns_to_cycles(200000000, 13) == 3); // 2.6 cycles
    CHECK(timing_ns_to_cycles(200000000, 0xFFFFFFFFu) == 858993459ull);

    CHECK(timing_pulse(200, 0, 1, &high, &low) && high == 197);
    CHECK(!timing_pulse(2, 0, 1, &high, &low));
    CHECK(timing_pulse(3, 0, 1, &high, &low) && high == 0);
    CHECK(timing_pulse(200, 1000, 5, &high, &low) && high == 197 && low == 796);
    CHECK(timing_pulse(200, 204, 5, &high, &low) && low == 0);
    CHECK(!timing_pulse(200, 203, 5, &high, &low));
    CHECK(!timing_pulse(200, 1000, 0, &high, &low));
}

static void test_pattern_timing(void)
{
    PATTERN_TIMING timing;

    CHECK(timing_pattern(200000000, 100000000.0f, &timing)); // 2 cycles
    CHECK(timing.fast && timing.divInt == 1 && timing.divFrac == 0 && timing.actual == 100000000.0);
    CHECK(!timing_pattern(200000000, 150000000.0f, &timing));

    CHECK(timing_pattern(200000000, 1000000.0f, &timing)); // 200 cycles
    CHECK(!timing.fast && timing.divInt == 1 && timing.delay == 196 && timing.actual == 1000000.0);

    CHECK(timing_pattern(200000000, 3000000.0f, &timing)); // 66.7 cycles -> 67
    CHECK(timing.delay == 63 && fabs(timing.actual - 200000000.0 / 67) < 1e-6);

    CHECK(timing_pattern(200000000, 0.01f, &timing)); // 2e10 cycles: the divider helps
    CHECK(timing.divInt == 5 && fabs(timing.actual - 0.01) < 1e-6);
    CHECK(!timing_pattern(200000000, 0.0f, &timing));
}

static void test_adc(void)
{
    uint16_t divInt;
    uint8_t divFrac;

    CHECK(timing_adc(500000, 1, &divInt, &divFrac) == 500000000u && divInt == 0 && divFrac == 0);
    CHECK(timing_adc(500000, 3, &divInt, &divFrac) == 166666667u && divInt == 0);
    CHECK(timing_adc(100000, 1, &divInt, &divFrac) == 100000000u && divInt == 479 && divFrac == 0);
    CHECK(timing_adc(1000, 2, &divInt, &divFrac) == 1000000u && divInt == 23999);
    CHECK(timing_adc(1, 1, &divInt, &divFrac) > 700000u && divInt == 65535 && divFrac == 255); // slowest
    CHECK(timing_adc(0, 1, &divInt, &divFrac) == 0);
    CHECK(timing_adc(1000, 0, &divInt, &divFrac) == 0);
    CHECK(timing_adc(1000, 5, &divInt, &divFrac) == 0);
}

static void test_state_line(void)
{
    char line[96];
    uint16_t adc[] = { 0, 4095, 123 };

    timing_format_state(line, sizeof(line), 1234567ull, 0x1C, NULL, 0);
    CHECK(strcmp(line, "STATE:1234567,1C\n") == 0);

    timing_format_state(line, sizeof(line), 5000000000ull, 0x3FFFFFFF, adc, 3);
    CHECK(strcmp(line, "STATE:5000000000,3FFFFFFF,0,4095,123\n") == 0);
}

int main(void)
{
    test_frames();
    test_allowed_while_capturing();
    test_pattern_data();
    test_pwm();
    test_pulse();
    test_pattern_timing();
    test_adc();
    test_state_line();

    printf("%d checks, %d failed\n", checks, failures);
    return failures ? 1 : 0;
}
