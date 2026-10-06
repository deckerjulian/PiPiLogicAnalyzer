/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef __OPENSCILAB_MONITOR__
#define __OPENSCILAB_MONITOR__

//Monitor (command 17): reports "STATE:<time µs>,<levels hex>[,<adc raw>...]" at a fixed rate,
//alongside everything else (also while a buffer capture runs, not during a stream).

#include "pico/stdlib.h"

//Highest rate of the reports, mHz (1 kHz)
#define MONITOR_MAX_MILLI_HZ 1000000u

/// @brief Starts (rate > 0) or stops (rate 0) the reports
/// @param toWiFi The reports go to the WiFi connection
/// @return NULL or the error answer
const char* monitor_start(uint32_t rateMilliHz, uint32_t mask, uint8_t adcMask, bool toWiFi);

void monitor_stop(void);

bool monitor_running(void);

/// @brief Sends a report when one is due (main loop)
void monitor_step(void (*send)(const char* line, bool toWiFi));

#endif
