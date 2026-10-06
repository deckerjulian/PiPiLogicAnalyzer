/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

// Host test of the pin table (pins.c), compiled once per board (BUILD_<board>).

#include <stdio.h>
#include <string.h>
#include "board_settings.h"
#include "pins.h"

static int failures = 0;
static int checks = 0;

#define CHECK(condition) do { checks++; if(!(condition)) { failures++; printf("%s [%s]:%d: CHECK failed: %s\n", __FILE__, TEST_BOARD, __LINE__, #condition); } } while(0)

static int count_char(const char* text, char wanted)
{
    int count = 0;
    for(; *text; text++)
        count += *text == wanted;
    return count;
}

int main(void)
{
    static const uint8_t pinMap[] = PIN_MAP;
    char line[160];

    CHECK(pins_count() == PIN_GPIO_COUNT);
    CHECK(pins_info(PIN_GPIO_COUNT) == NULL);
    CHECK(pins_format(PIN_GPIO_COUNT, line, sizeof(line)) < 0);

    // Every channel of the pin map is on its GPIO, free and can clock the state mode
    for(uint8_t channel = 0; channel < MAX_CHANNELS; channel++)
    {
        const PIN_INFO* info = pins_info(pinMap[channel]);
        CHECK(info != NULL);
        CHECK(info->channel == channel);
        CHECK(info->reserved == NULL);
        CHECK((info->caps & (PIN_CAN_DIN | PIN_CAN_DOUT | PIN_CAN_PWM | PIN_CAN_CLOCK)) == (PIN_CAN_DIN | PIN_CAN_DOUT | PIN_CAN_PWM | PIN_CAN_CLOCK));
    }

    // The trigger pins of a multi-board set are reserved
    CHECK(pins_info(COMPLEX_TRIGGER_OUT_PIN)->reserved != NULL);
    CHECK(pins_info(COMPLEX_TRIGGER_IN_PIN)->reserved != NULL);
    CHECK(pins_info(COMPLEX_TRIGGER_OUT_PIN)->channel == PIN_NO_CHANNEL);
    CHECK(pins_reserved_mask() & (1u << COMPLEX_TRIGGER_OUT_PIN));
    CHECK(pins_valid_mask() == 0x3FFFFFFFu);

    // The square wave output (GP22) is a free PWM pin
    CHECK(pins_info(22)->reserved == NULL && (pins_info(22)->caps & PIN_CAN_PWM));

    // Every line has the fields of docs/protocols.md
    for(uint8_t gpio = 0; gpio < pins_count(); gpio++)
    {
        int length = pins_format(gpio, line, sizeof(line));
        CHECK(length > 0 && length < (int)sizeof(line));

        char prefix[16];
        snprintf(prefix, sizeof(prefix), "PIN:GP%u,", (unsigned)gpio);
        CHECK(strncmp(line, prefix, strlen(prefix)) == 0);

        int commas = count_char(line, ',');
        CHECK(commas == (pins_info(gpio)->reserved ? 5 : 4));
        if(pins_info(gpio)->reserved)
            CHECK(strstr(line, ",reserved: ") != NULL);
        CHECK(strstr(line, ",3300,") != NULL);
        CHECK(strchr(line, '\n') == NULL);
    }

    // Analog inputs
    #if defined (BUILD_ZERO) || defined (BUILD_INTERCEPTOR)
        CHECK(pins_adc_count() == 4);
        CHECK(pins_adc_mask() == 0x0F);
    #else
        CHECK(pins_adc_count() == 3);
        CHECK(pins_adc_mask() == 0x07);
        CHECK(pins_info(29)->reserved != NULL);
        CHECK(pins_info(25)->reserved != NULL);
        CHECK(pins_info(23)->reserved != NULL);
        CHECK(pins_info(24)->reserved != NULL);
    #endif

    // The lines of the examples in docs/protocols.md (Pico boards)
    #if defined (BUILD_PICO) || defined (BUILD_PICO_2) || defined (BUILD_PICO_W) || defined (BUILD_PICO_W_WIFI) || defined (BUILD_PICO_2_W) || defined (BUILD_PICO_2_W_WIFI)
        pins_format(0, line, sizeof(line));
        CHECK(strcmp(line, "PIN:GP0,DIN/DOUT,-,3300,-,reserved: trigger output of a multi-board set") == 0);
        pins_format(2, line, sizeof(line));
        CHECK(strcmp(line, "PIN:GP2,DIN/DOUT/PULLUP/PULLDOWN/PWM/CLOCK,0,3300,-") == 0);
        pins_format(26, line, sizeof(line));
        CHECK(strcmp(line, "PIN:GP26,DIN/DOUT/PULLUP/PULLDOWN/PWM/ADC/CLOCK,21,3300,0") == 0);
        pins_format(23, line, sizeof(line));
        CHECK(strstr(line, ",-,3300,-,reserved: ") != NULL);
    #endif

    #if defined (BUILD_ZERO)
        CHECK(pins_info(LED_IO)->reserved != NULL);
        CHECK(pins_info(19)->reserved == NULL && pins_info(19)->channel == PIN_NO_CHANNEL);
    #endif

    printf("%s: %d checks, %d failed\n", TEST_BOARD, checks, failures);
    return failures ? 1 : 0;
}
