/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "timing.h"
#include <math.h>
#include <stdio.h>

//Longest PWM period in counts: top + 1 must fit the 16 bit compare value (always high = top + 1)
#define PWM_MAX_COUNTS 65535.0
#define PWM_MAX_DIV16 4095u

bool timing_pwm(uint32_t systemClock, float frequency, PWM_TIMING* timing)
{
    if(!(frequency > 0.0f) || isinf(frequency))
        return false;

    double cycles = (double)systemClock / (double)frequency;
    double div16 = ceil(cycles * 16.0 / PWM_MAX_COUNTS);

    if(div16 < 16.0)
        div16 = 16.0;

    if(div16 > PWM_MAX_DIV16)
        return false;

    double counts = floor(cycles * 16.0 / div16 + 0.5);

    if(counts < 2.0)
        return false;

    if(counts > PWM_MAX_COUNTS)
        counts = PWM_MAX_COUNTS;

    timing->div16 = (uint16_t)div16;
    timing->top = (uint16_t)(counts - 1.0);
    timing->actual = (double)systemClock * 16.0 / (div16 * counts);
    return true;
}

uint32_t timing_pwm_level(uint16_t duty, uint16_t top)
{
    return (uint32_t)(((uint64_t)duty * ((uint32_t)top + 1) + 32767u) / 65535u);
}

uint64_t timing_ns_to_cycles(uint32_t systemClock, uint32_t ns)
{
    return ((uint64_t)ns * systemClock + 500000000u) / 1000000000u;
}

bool timing_pulse(uint64_t highCycles, uint64_t periodCycles, uint16_t count, uint32_t* highDelay, uint32_t* lowDelay)
{
    if(count == 0 || highCycles < TIMING_PULSE_HIGH_CYCLES || highCycles - TIMING_PULSE_HIGH_CYCLES > 0xFFFFFFFFu)
        return false;

    *highDelay = (uint32_t)(highCycles - TIMING_PULSE_HIGH_CYCLES);

    if(count == 1)
    {
        *lowDelay = 0; //The gap after the last pulse is not seen
        return true;
    }

    if(periodCycles < highCycles + TIMING_PULSE_LOW_CYCLES || periodCycles - highCycles - TIMING_PULSE_LOW_CYCLES > 0xFFFFFFFFu)
        return false;

    *lowDelay = (uint32_t)(periodCycles - highCycles - TIMING_PULSE_LOW_CYCLES);
    return true;
}

bool timing_pattern(uint32_t systemClock, float rate, PATTERN_TIMING* timing)
{
    if(!(rate > 0.0f) || isinf(rate))
        return false;

    double cycles = (double)systemClock / (double)rate;

    if(cycles < TIMING_PATTERN_FAST_CYCLES)
        return false;

    if(cycles < TIMING_PATTERN_SLOW_CYCLES)
    {
        //Fast variant: 2 cycles per sample, the divider stretches them (fractional: a cycle of jitter)
        uint32_t div256 = (uint32_t)floor(cycles / TIMING_PATTERN_FAST_CYCLES * 256.0 + 0.5);

        if(div256 < 256)
            div256 = 256;

        timing->fast = true;
        timing->divInt = (uint16_t)(div256 >> 8);
        timing->divFrac = (uint8_t)(div256 & 0xFF);
        timing->delay = 0;
        timing->actual = (double)systemClock * 256.0 / ((double)div256 * TIMING_PATTERN_FAST_CYCLES);
        return true;
    }

    //Delay variant: a whole number of cycles per sample, the divider only for very low rates
    double divider = ceil(cycles / (4294967295.0 + TIMING_PATTERN_SLOW_CYCLES));

    if(divider < 1.0)
        divider = 1.0;

    if(divider > 65535.0)
        return false;

    double perSample = floor(cycles / divider + 0.5);

    if(perSample < TIMING_PATTERN_SLOW_CYCLES)
        perSample = TIMING_PATTERN_SLOW_CYCLES;

    timing->fast = false;
    timing->divInt = (uint16_t)divider;
    timing->divFrac = 0;
    timing->delay = (uint32_t)(perSample - TIMING_PATTERN_SLOW_CYCLES);
    timing->actual = (double)systemClock / (divider * perSample);
    return true;
}

uint32_t timing_adc(uint32_t ratePerChannel, uint8_t channels, uint16_t* divInt, uint8_t* divFrac)
{
    if(ratePerChannel == 0 || channels == 0 || channels > 4)
        return 0;

    uint64_t total = (uint64_t)ratePerChannel * channels;
    uint64_t div256;

    if(total >= TIMING_ADC_MAX_RATE)
        div256 = 0; //Back to back
    else
    {
        //Period of (1 + DIV) ADC clock cycles
        div256 = ((uint64_t)TIMING_ADC_CLOCK * 256u + total / 2) / total;
        div256 = div256 > 256 ? div256 - 256 : 0;

        if(div256 < (uint64_t)(TIMING_ADC_CYCLES - 1) * 256u)
            div256 = 0;

        if(div256 > 0xFFFFFFu)
            div256 = 0xFFFFFFu; //Slowest the divider can do
    }

    *divInt = (uint16_t)(div256 >> 8);
    *divFrac = (uint8_t)(div256 & 0xFF);

    //Conversions per second of all inputs together, then per channel, in mHz
    uint64_t period256 = div256 ? div256 + 256u : (uint64_t)TIMING_ADC_CYCLES * 256u;
    return (uint32_t)(((uint64_t)TIMING_ADC_CLOCK * 256000u + period256 * channels / 2) / (period256 * channels));
}

int timing_format_state(char* buffer, size_t size, uint64_t microseconds, uint32_t levels, const uint16_t* adc, uint8_t adcCount)
{
    int used = snprintf(buffer, size, "STATE:%llu,%lX", (unsigned long long)microseconds, (unsigned long)levels);

    for(uint8_t i = 0; i < adcCount && used >= 0 && (size_t)used < size; i++)
        used += snprintf(buffer + used, size - used, ",%u", (unsigned)adc[i]);

    if(used >= 0 && (size_t)used + 1 < size)
    {
        buffer[used++] = '\n';
        buffer[used] = 0;
    }

    return used;
}
