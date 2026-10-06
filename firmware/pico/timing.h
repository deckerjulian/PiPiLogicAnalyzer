/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef __OPENSCILAB_TIMING__
#define __OPENSCILAB_TIMING__

//Dividers and cycle counts of the outputs and the ADC: plain C without Pico SDK dependencies,
//compiled on the host by tests/ as well.

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

//ADC: 48 MHz clock, 96 cycles per conversion (500 kSa/s for all inputs together)
#define TIMING_ADC_CLOCK 48000000u
#define TIMING_ADC_CYCLES 96u
#define TIMING_ADC_MAX_RATE (TIMING_ADC_CLOCK / TIMING_ADC_CYCLES)

//Pulse program (pulse.pio): cycles of a pulse and of the gap between two pulses beyond the delay
//loops, i.e. the shortest pulse and the shortest gap
#define TIMING_PULSE_HIGH_CYCLES 3u
#define TIMING_PULSE_LOW_CYCLES 4u

//Pattern program (pattern.pio): cycles per sample of the fast variant and of the delay variant
//without delay
#define TIMING_PATTERN_FAST_CYCLES 2u
#define TIMING_PATTERN_SLOW_CYCLES 4u

typedef struct _PWM_TIMING
{
    uint16_t div16;     //Clock divider in 1/16 (8.4 fixed point, 16..4095)
    uint16_t top;       //Counter wrap value: the period is top + 1 counts
    double actual;      //Resulting frequency, Hz

} PWM_TIMING;

typedef struct _PATTERN_TIMING
{
    bool fast;          //Fast variant (2 cycles per sample, fractional divider) or delay loop
    uint16_t divInt;    //PIO clock divider, integer part (1..65535)
    uint8_t divFrac;    //PIO clock divider, 1/256
    uint32_t delay;     //Delay variant: loop count (cycles per sample = TIMING_PATTERN_SLOW_CYCLES + delay)
    double actual;      //Resulting sample rate, Hz

} PATTERN_TIMING;

/// @brief Divider and wrap of a PWM slice for a frequency
/// @return False if the frequency cannot be made (above half the system clock, below the divider range)
bool timing_pwm(uint32_t systemClock, float frequency, PWM_TIMING* timing);

/// @brief Compare level of a PWM channel for a duty cycle of 0..65535 (65535: always high)
uint32_t timing_pwm_level(uint16_t duty, uint16_t top);

/// @brief Nanoseconds to system clock cycles, rounded
uint64_t timing_ns_to_cycles(uint32_t systemClock, uint32_t ns);

/// @brief Delay counts of the pulse program
/// @param highCycles Width of a pulse in cycles
/// @param periodCycles Period in cycles (ignored for a single pulse)
/// @param count Number of pulses
/// @param highDelay Set to the delay loop count of the pulse
/// @param lowDelay Set to the delay loop count of the gap
/// @return False if the pulse is too short or the gap between pulses too short
bool timing_pulse(uint64_t highCycles, uint64_t periodCycles, uint16_t count, uint32_t* highDelay, uint32_t* lowDelay);

/// @brief Clock divider and delay of the pattern program for a sample rate
/// @return False if the rate cannot be made
bool timing_pattern(uint32_t systemClock, float rate, PATTERN_TIMING* timing);

/// @brief ADC divider for a rate per channel with round robin over several inputs
/// @param ratePerChannel Requested rate per channel, Hz (limited to what the ADC can do)
/// @param channels Inputs sampled in turn (1..4)
/// @param divInt Set to the integer part of the ADC divider (0: back to back conversions)
/// @param divFrac Set to the fractional part (1/256)
/// @return Actual rate per channel in mHz, 0 if the arguments are invalid
uint32_t timing_adc(uint32_t ratePerChannel, uint8_t channels, uint16_t* divInt, uint8_t* divFrac);

/// @brief Formats a monitor report "STATE:<µs>,<levels hex>[,<adc>...]" with a line end
/// @return Characters written (as snprintf)
int timing_format_state(char* buffer, size_t size, uint64_t microseconds, uint32_t levels, const uint16_t* adc, uint8_t adcCount);

#endif
