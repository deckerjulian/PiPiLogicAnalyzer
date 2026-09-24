/*
 * Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
 * Copyright (C) 2026 Julian Decker
 *
 * Part of PiPiLogicAnalyzer, based on his LogicAnalyzer firmware;
 * the changes are described in firmware/README.md.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "PiPiLogicAnalyzer_Board_Settings.h"

#include <stdio.h>
#include <stdbool.h>
#include "pico/stdlib.h"
#include "hardware/gpio.h"
#include "hardware/clocks.h"
#include "PiPiLogicAnalyzer_Capture.h"
#include "PiPiLogicAnalyzer_SelfTest.h"

extern const uint8_t pinMap[];

//Time for a pull resistor to charge the pin capacitance
#define SETTLE_MS 2
//Samples recorded by the capture path tests
#define PATTERN_SAMPLES 1000
#define PATTERN_FREQUENCY 1000000
#define CAPTURE_TIMEOUT_MS 1000

static void report(SELFTEST_REPORT out, void* context, const char* item, const char* status, const char* detail)
{
    char line[192];
    snprintf(line, sizeof(line), "SELFTEST:%s:%s:%s\n", item, status, detail ? detail : "");
    out(line, context);
}

//Back to the reset state of the pad: no function, pull-down enabled
static void restore_pin(uint8_t pin)
{
    gpio_deinit(pin);
    gpio_set_pulls(pin, false, true);
}

static void test_board(SELFTEST_REPORT out, void* context)
{
    char detail[128];
    snprintf(detail, sizeof(detail), "%s %s, %d channels, %d byte buffer, %lu Hz clock",
        BOARD_NAME, FIRMWARE_VERSION, MAX_CHANNELS, CAPTURE_BUFFER_SIZE, (unsigned long)clock_get_hz(clk_sys));
    report(out, context, "BOARD", "INFO", detail);
}

static void test_ram(SELFTEST_REPORT out, void* context)
{
    char detail[64];
    uint32_t failedOffset = 0;

    if(TestCaptureBuffer(&failedOffset))
    {
        snprintf(detail, sizeof(detail), "%d bytes", CAPTURE_BUFFER_SIZE);
        report(out, context, "RAM", "OK", detail);
    }
    else
    {
        snprintf(detail, sizeof(detail), "pattern mismatch at offset %lu", (unsigned long)failedOffset);
        report(out, context, "RAM", "FAIL", detail);
    }
}

static void test_trigger_link(SELFTEST_REPORT out, void* context)
{
#ifdef SUPPORTS_COMPLEX_TRIGGER
    char detail[160];

    //The trigger output drives the trigger input, which is pulled to the opposite level
    gpio_init(COMPLEX_TRIGGER_OUT_PIN);
    gpio_set_dir(COMPLEX_TRIGGER_OUT_PIN, GPIO_OUT);
    gpio_init(COMPLEX_TRIGGER_IN_PIN);
    gpio_set_dir(COMPLEX_TRIGGER_IN_PIN, GPIO_IN);

    gpio_set_pulls(COMPLEX_TRIGGER_IN_PIN, false, true);
    gpio_put(COMPLEX_TRIGGER_OUT_PIN, 1);
    busy_wait_ms(SETTLE_MS);
    bool followsHigh = gpio_get(COMPLEX_TRIGGER_IN_PIN);

    gpio_set_pulls(COMPLEX_TRIGGER_IN_PIN, true, false);
    gpio_put(COMPLEX_TRIGGER_OUT_PIN, 0);
    busy_wait_ms(SETTLE_MS);
    bool followsLow = !gpio_get(COMPLEX_TRIGGER_IN_PIN);

    restore_pin(COMPLEX_TRIGGER_OUT_PIN);
    restore_pin(COMPLEX_TRIGGER_IN_PIN);

    if(followsHigh && followsLow)
    {
        snprintf(detail, sizeof(detail), "GPIO %d -> GPIO %d", COMPLEX_TRIGGER_OUT_PIN, COMPLEX_TRIGGER_IN_PIN);
        report(out, context, "TRIGGER_LINK", "OK", detail);
    }
    else
    {
        snprintf(detail, sizeof(detail),
            "GPIO %d and GPIO %d are not connected: pattern triggers and multi device chaining will not work",
            COMPLEX_TRIGGER_OUT_PIN, COMPLEX_TRIGGER_IN_PIN);
        report(out, context, "TRIGGER_LINK", "FAIL", detail);
    }
#else
    report(out, context, "TRIGGER_LINK", "SKIPPED", "the board has no pattern trigger");
#endif
}

//Checks every channel input with the pull resistors, returns the channels that follow them
static uint32_t test_channels(SELFTEST_REPORT out, void* context)
{
    uint32_t followingChannels = 0;
    char item[16];
    char detail[32];

    for(uint8_t channel = 0; channel < MAX_CHANNELS; channel++)
    {
        uint8_t pin = pinMap[channel];

        gpio_init(pin);
        gpio_set_dir(pin, GPIO_IN);

        gpio_set_pulls(pin, true, false);
        busy_wait_ms(SETTLE_MS);
        bool high = gpio_get(pin);

        gpio_set_pulls(pin, false, true);
        busy_wait_ms(SETTLE_MS);
        bool low = !gpio_get(pin);

        restore_pin(pin);

        const char* status;

        if(high && low)
        {
            status = "OK";
            followingChannels |= 1u << channel;
        }
        else if(low)
            status = "STUCK_LOW";
        else if(high)
            status = "STUCK_HIGH";
        else
            status = "INVERTED";

        snprintf(item, sizeof(item), "CH%d", channel + 1);
        snprintf(detail, sizeof(detail), "GPIO %d", pin);
        report(out, context, item, status, detail);
    }

    return followingChannels;
}

//Records the pull pattern (even channels high, odd channels low) through a capture path
static void test_capture_path(SELFTEST_REPORT out, void* context, uint32_t usableChannels, bool blast)
{
    const char* item = blast ? "BLAST_CAPTURE" : "CAPTURE";
    char detail[128];

    //Channel 1 is the trigger, it must follow its pull-up
    if(!(usableChannels & 1))
    {
        report(out, context, item, "SKIPPED", "channel 1 does not follow the pull resistors");
        return;
    }

    uint8_t channels[MAX_CHANNELS];
    uint32_t expected = 0;

    for(uint8_t channel = 0; channel < MAX_CHANNELS; channel++)
    {
        bool up = (channel % 2) == 0;
        channels[channel] = channel;
        gpio_set_pulls(pinMap[channel], up, !up);

        if(up)
            expected |= 1u << channel;
    }

    busy_wait_ms(SETTLE_MS);

    uint32_t frequency = blast ? MAX_BLAST_FREQ : PATTERN_FREQUENCY;
    bool started = blast ?
        StartCaptureBlast(frequency, PATTERN_SAMPLES, channels, MAX_CHANNELS, 0, false, MODE_24_CHANNEL) :
        StartCaptureSimple(frequency, 0, PATTERN_SAMPLES, 0, 0, channels, MAX_CHANNELS, 0, false, MODE_24_CHANNEL);

    if(started)
    {
        absolute_time_t deadline = make_timeout_time_ms(CAPTURE_TIMEOUT_MS);

        while(IsCapturing() && !time_reached(deadline))
            busy_wait_ms(1);

        if(IsCapturing())
        {
            StopCapture();
            report(out, context, item, "FAIL", "no data within 1 s (PIO/DMA did not complete)");
        }
        else
        {
            uint32_t length;
            uint32_t first;
            CHANNEL_MODE mode;
            uint32_t* samples = (uint32_t*)GetBuffer(&length, &first, &mode);
            uint32_t maxSamples = CAPTURE_BUFFER_SIZE / 4;
            uint32_t mismatches = 0;
            uint32_t firstMismatch = 0;

            for(uint32_t sample = 0; sample < length; sample++)
            {
                uint32_t value = samples[(first + sample) % maxSamples] & usableChannels;

                if(value != (expected & usableChannels))
                {
                    if(mismatches == 0)
                        firstMismatch = value;

                    mismatches++;
                }
            }

            if(mismatches == 0)
            {
                snprintf(detail, sizeof(detail), "%lu samples at %lu Hz", (unsigned long)length, (unsigned long)frequency);
                report(out, context, item, "OK", detail);
            }
            else
            {
                snprintf(detail, sizeof(detail), "%lu of %lu samples differ (0x%08lX instead of 0x%08lX)",
                    (unsigned long)mismatches, (unsigned long)length, (unsigned long)firstMismatch,
                    (unsigned long)(expected & usableChannels));
                report(out, context, item, "FAIL", detail);
            }
        }
    }
    else
        report(out, context, item, "FAIL", "the capture could not be started");

    for(uint8_t channel = 0; channel < MAX_CHANNELS; channel++)
        restore_pin(pinMap[channel]);
}

void RunSelfTest(SELFTEST_REPORT out, void* context)
{
    test_board(out, context);
    test_ram(out, context);
    test_trigger_link(out, context);

    uint32_t usableChannels = test_channels(out, context);

    test_capture_path(out, context, usableChannels, false);
    test_capture_path(out, context, usableChannels, true);

    out("SELFTEST_END\n", context);
}
