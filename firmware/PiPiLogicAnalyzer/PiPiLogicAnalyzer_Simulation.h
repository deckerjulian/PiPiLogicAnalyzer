/*
 * Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
 * Copyright (C) 2026 Julian Decker
 *
 * Part of PiPiLogicAnalyzer, based on his LogicAnalyzer firmware;
 * the changes are described in firmware/README.md.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef __LOGICANALYZER_SIMULATION__
#define __LOGICANALYZER_SIMULATION__

//Test signal generator for the simulated capture (trigger type 4).
//
//Plain C without Pico SDK dependencies, so it can be compiled on a PC and compared with the
//generator of the application (pipilogicanalyzer/core/simulation.py). Both must stay identical.

#include <stdint.h>

//Pattern, transmitted in the triggerValue field of the capture request
typedef enum
{
    SIM_PATTERN_COUNTER = 0,        //Channel n toggles every 2^(n % 16) samples (binary counter)
    SIM_PATTERN_WALKING_ONE = 1,    //One channel high at a time, SIM_WALK_STEP samples each
    SIM_PATTERN_PROTOCOLS = 2       //UART, SPI and I2C frames carrying SIM_MESSAGE, counter on the rest

} SIM_PATTERN;

#define SIM_PATTERN_COUNT 3

//Text carried by the protocol frames
#define SIM_MESSAGE "PiPiLogicAnalyzer\n"

//Walking one: samples per channel
#define SIM_WALK_STEP 16

//UART (channel 1): 8N1, LSB first, samples per bit and idle samples between messages
#define SIM_UART_BIT 10
#define SIM_UART_IDLE 200

//SPI (channels 2-4: CLK, MOSI, CS): mode 0, MSB first, CS active low
#define SIM_SPI_HALF 5
#define SIM_SPI_GAP 100

//I2C (channels 5-6: SCL, SDA): write of SIM_MESSAGE to SIM_I2C_ADDRESS, every bit lasts 4 quarters
#define SIM_I2C_QUARTER 5
#define SIM_I2C_GAP 100
#define SIM_I2C_ADDRESS 0x50

/// @brief Computes one sample of a simulated capture
/// @param pattern Pattern to generate (SIM_PATTERN)
/// @param channelCount Number of captured channels (bit n of the result is the n-th channel)
/// @param index Sample number
/// @return Channel levels of the sample
uint32_t simulation_sample(uint8_t pattern, uint8_t channelCount, uint32_t index);

#endif
