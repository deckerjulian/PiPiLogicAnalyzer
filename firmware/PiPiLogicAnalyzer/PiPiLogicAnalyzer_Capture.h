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
#ifndef __ANALYZER_CAPTURE__
#define __ANALYZER_CAPTURE__

#if defined(CORE_TYPE_2) //All RP2350 boards (Pico 2, Pico 2 W), not only BUILD_PICO_2
#include <RP2350.h>
#endif

typedef enum
{
    MODE_8_CHANNEL,
    MODE_16_CHANNEL,
    MODE_24_CHANNEL

} CHANNEL_MODE;

bool StartCaptureSimple(uint32_t freq, uint32_t preLength, uint32_t postLength, uint16_t loopCount, uint8_t measureBursts, const uint8_t* capturePins, uint8_t capturePinCount, uint8_t triggerPin, bool invertTrigger, CHANNEL_MODE captureMode);
bool StartCaptureBlast(uint32_t freq, uint32_t length, const uint8_t* capturePins, uint8_t capturePinCount, uint8_t triggerPin, bool invertTrigger, CHANNEL_MODE captureMode);
#ifdef SUPPORTS_COMPLEX_TRIGGER
bool StartCaptureComplex(uint32_t freq, uint32_t preLength, uint32_t postLength, const uint8_t* capturePins, uint8_t capturePinCount, uint8_t triggerPinBase, uint8_t triggerPinCount, uint16_t triggerValue, CHANNEL_MODE captureMode);
bool StartCaptureFast(uint32_t freq, uint32_t preLength, uint32_t postLength, const uint8_t* capturePins, uint8_t capturePinCount, uint8_t triggerPinBase, uint8_t triggerPinCount, uint16_t triggerValue, CHANNEL_MODE captureMode);
//Edge trigger that also drives the trigger output (trigger type 5)
bool StartCaptureEdgeOut(uint32_t freq, uint32_t preLength, uint32_t postLength, const uint8_t* capturePins, uint8_t capturePinCount, uint8_t triggerPin, bool invertTrigger, CHANNEL_MODE captureMode);
//Channel groups on consecutive GPIOs usable by a pattern trigger, e.g. "0-20/21-23"
void GetPatternTriggerGroups(char* buffer, uint32_t size);
#endif
//Simulated capture (trigger type 4): test signals instead of sampled pins
bool StartCaptureSimulation(uint32_t freq, uint32_t preLength, uint32_t postLength, const uint8_t* capturePins, uint8_t capturePinCount, uint8_t pattern, CHANNEL_MODE captureMode);
//Pattern test of the capture buffer (clears it afterwards)
bool TestCaptureBuffer(uint32_t* failedOffset);
void StopCapture();
bool IsCapturing();
uint8_t* GetBuffer(uint32_t* bufferSize, uint32_t* firstSample, CHANNEL_MODE* captureMode);
volatile uint32_t* GetTimestamps(uint8_t* length);
void check_fast_interrupt();

#endif