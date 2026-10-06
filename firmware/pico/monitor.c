/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "monitor.h"
#include "pins.h"
#include "timing.h"
#include "analog.h"
#include "gpio_ctrl.h"
#include "hardware/gpio.h"

static bool running;
static bool wifi;
static uint32_t gpioMask;
static uint8_t adcMask;
static uint64_t periodUs;
static uint64_t next;

const char* monitor_start(uint32_t rateMilliHz, uint32_t mask, uint8_t adc, bool toWiFi)
{
    if(rateMilliHz == 0)
    {
        monitor_stop();
        return NULL;
    }

    if(rateMilliHz > MONITOR_MAX_MILLI_HZ)
        return ANSWER_ERR_ARG;

    if(mask & ~pins_valid_mask())
        return ANSWER_ERR_PIN;

    if(adc & ~pins_adc_mask())
        return ANSWER_ERR_PIN;

    gpioMask = mask;
    adcMask = adc;
    wifi = toWiFi;
    periodUs = (1000000000ull + rateMilliHz / 2) / rateMilliHz;
    next = time_us_64();
    running = true;
    return NULL;
}

void monitor_stop(void)
{
    running = false;
}

bool monitor_running(void)
{
    return running;
}

void monitor_step(void (*send)(const char* line, bool toWiFi))
{
    if(!running)
        return;

    uint64_t now = time_us_64();
    if(now < next)
        return;

    uint16_t values[PIN_ADC_COUNT];
    uint8_t count = 0;
    uint32_t levels = gpio_get_all() & gpioMask;

    if(adcMask && analog_read(adcMask, values, &count, false) != NULL)
        count = 0;

    char line[96];
    timing_format_state(line, sizeof(line), now, levels, values, count);
    send(line, wifi);

    //Fixed rate; when the board fell behind (a long transfer) it continues from now
    next += periodUs;
    if(next <= now)
        next = now + periodUs;
}
