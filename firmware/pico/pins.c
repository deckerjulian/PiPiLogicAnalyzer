/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "board_settings.h"
#include "capabilities.h"
#include "pins.h"
#include <stdio.h>
#include <string.h>

#define REASON_TRIGGER_OUT "trigger output of a multi-board set"
#define REASON_TRIGGER_IN "trigger input of a multi-board set"

static PIN_INFO table[PIN_GPIO_COUNT];
static bool built = false;

static void reserve(uint8_t gpio, uint8_t caps, const char* reason)
{
    if(gpio >= PIN_GPIO_COUNT)
        return;

    table[gpio].caps = caps;
    table[gpio].reserved = reason;
}

static void build(void)
{
    static const uint8_t pinMap[] = PIN_MAP;

    for(uint8_t gpio = 0; gpio < PIN_GPIO_COUNT; gpio++)
    {
        table[gpio].caps = PIN_CAN_DIN | PIN_CAN_DOUT | PIN_CAN_PULLUP | PIN_CAN_PULLDOWN | PIN_CAN_PWM;
        table[gpio].channel = PIN_NO_CHANNEL;
        table[gpio].reserved = NULL;
    }

    //Capture channels: the first MAX_CHANNELS entries of the pin map (the last one is the trigger input)
    for(uint8_t channel = 0; channel < MAX_CHANNELS; channel++)
    {
        uint8_t gpio = pinMap[channel];

        if(gpio >= PIN_GPIO_COUNT || table[gpio].channel != PIN_NO_CHANNEL)
            continue;

        table[gpio].channel = channel;

        //The state mode waits for the clock with WAIT PIN, relative to the first sampled GPIO
        //(as int: with INPUT_PIN_BASE 0 an unsigned comparison would always be true)
        int offset = (int)gpio - (int)INPUT_PIN_BASE;
        if(offset >= 0 && offset < 32)
            table[gpio].caps |= PIN_CAN_CLOCK;
    }

    #ifdef SUPPORTS_COMPLEX_TRIGGER
        reserve(COMPLEX_TRIGGER_OUT_PIN, PIN_CAN_DIN | PIN_CAN_DOUT, REASON_TRIGGER_OUT);
        reserve(COMPLEX_TRIGGER_IN_PIN, PIN_CAN_DIN, REASON_TRIGGER_IN);
    #endif

    //Pins wired on the board
    #if defined (CYGW_LED)
        //Pico W, Pico 2 W: the WiFi chip (its SPI and power; GPIO 29 also measures VSYS)
        reserve(23, PIN_CAN_DOUT, "power of the WiFi chip");
        reserve(24, PIN_CAN_DIN | PIN_CAN_DOUT, "data of the WiFi chip");
        reserve(25, PIN_CAN_DOUT, "chip select of the WiFi chip");
        reserve(29, PIN_CAN_DOUT, "clock of the WiFi chip");
    #elif defined (BUILD_PICO) || defined (BUILD_PICO_2)
        reserve(23, PIN_CAN_DOUT, "power save mode of the supply");
        reserve(24, PIN_CAN_DIN, "VBUS sense");
        reserve(LED_IO, PIN_CAN_DOUT, "on-board LED");
        reserve(29, PIN_CAN_DIN | PIN_CAN_ADC, "VSYS measurement");
    #elif defined (WS2812_LED)
        reserve(LED_IO, PIN_CAN_DOUT, "on-board RGB LED");
    #elif defined (GPIO_LED)
        reserve(LED_IO, PIN_CAN_DOUT, "on-board LED");
    #endif

    //ADC inputs
    for(uint8_t gpio = PIN_ADC_BASE; gpio < PIN_ADC_BASE + PIN_ADC_COUNT && gpio < PIN_GPIO_COUNT; gpio++)
        if(table[gpio].reserved == NULL)
            table[gpio].caps |= PIN_CAN_ADC;

    built = true;
}

const PIN_INFO* pins_info(uint8_t gpio)
{
    if(!built)
        build();

    return gpio < PIN_GPIO_COUNT ? &table[gpio] : NULL;
}

uint8_t pins_count(void)
{
    return PIN_GPIO_COUNT;
}

int pins_format(uint8_t index, char* buffer, size_t size)
{
    static const struct { uint8_t bit; const char* name; } names[] = {
        { PIN_CAN_DIN, PINCAP_DIN },
        { PIN_CAN_DOUT, PINCAP_DOUT },
        { PIN_CAN_PULLUP, PINCAP_PULLUP },
        { PIN_CAN_PULLDOWN, PINCAP_PULLDOWN },
        { PIN_CAN_PWM, PINCAP_PWM },
        { PIN_CAN_ADC, PINCAP_ADC },
        { PIN_CAN_CLOCK, PINCAP_CLOCK },
    };

    const PIN_INFO* info = pins_info(index);

    if(info == NULL || size == 0)
        return -1;

    char caps[64] = "";

    for(size_t i = 0; i < sizeof(names) / sizeof(names[0]); i++)
    {
        if(!(info->caps & names[i].bit))
            continue;

        if(caps[0])
            strcat(caps, "/");
        strcat(caps, names[i].name);
    }

    char channel[4] = "-";
    if(info->channel != PIN_NO_CHANNEL)
        snprintf(channel, sizeof(channel), "%u", (unsigned)info->channel);

    char analog[4] = "-";
    if(info->caps & PIN_CAN_ADC)
        snprintf(analog, sizeof(analog), "%u", (unsigned)(index - PIN_ADC_BASE));

    if(info->reserved)
        return snprintf(buffer, size, "PIN:GP%u,%s,%s,%d,%s,reserved: %s", (unsigned)index, caps, channel, PIN_LOGIC_MV, analog, info->reserved);

    return snprintf(buffer, size, "PIN:GP%u,%s,%s,%d,%s", (unsigned)index, caps, channel, PIN_LOGIC_MV, analog);
}

uint32_t pins_reserved_mask(void)
{
    uint32_t mask = 0;

    for(uint8_t gpio = 0; gpio < PIN_GPIO_COUNT; gpio++)
        if(pins_info(gpio)->reserved)
            mask |= 1u << gpio;

    return mask;
}

uint32_t pins_valid_mask(void)
{
    return (1u << PIN_GPIO_COUNT) - 1;
}

uint8_t pins_adc_mask(void)
{
    uint8_t mask = 0;

    for(uint8_t adc = 0; adc < PIN_ADC_COUNT; adc++)
    {
        const PIN_INFO* info = pins_info(PIN_ADC_BASE + adc);
        if(info && !info->reserved && (info->caps & PIN_CAN_ADC))
            mask |= 1u << adc;
    }

    return mask;
}

uint8_t pins_adc_count(void)
{
    uint8_t mask = pins_adc_mask();
    uint8_t count = 0;

    for(; mask; mask >>= 1)
        count += mask & 1;

    return count;
}
