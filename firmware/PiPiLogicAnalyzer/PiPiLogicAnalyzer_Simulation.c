/*
 * Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
 * Copyright (C) 2026 Julian Decker
 *
 * Part of PiPiLogicAnalyzer, based on his LogicAnalyzer firmware;
 * the changes are described in firmware/README.md.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "PiPiLogicAnalyzer_Simulation.h"

static const uint8_t message[] = SIM_MESSAGE;
#define MESSAGE_LENGTH ((uint32_t)(sizeof(message) - 1))

//UART, 8N1 LSB first: level of the TX line
static uint32_t uart_level(uint32_t index)
{
    uint32_t period = SIM_UART_IDLE + MESSAGE_LENGTH * 10 * SIM_UART_BIT;
    uint32_t t = index % period;

    if(t < SIM_UART_IDLE)
        return 1;

    uint32_t bit = (t - SIM_UART_IDLE) / SIM_UART_BIT;
    uint32_t position = bit % 10;

    if(position == 0) //Start bit
        return 0;

    if(position == 9) //Stop bit
        return 1;

    return (message[bit / 10] >> (position - 1)) & 1;
}

static uint32_t spi_bit(uint32_t bit)
{
    return (message[bit / 8] >> (7 - bit % 8)) & 1;
}

//SPI mode 0, MSB first: bit 0 = CLK, bit 1 = MOSI, bit 2 = CS
static uint32_t spi_levels(uint32_t index)
{
    uint32_t bits = MESSAGE_LENGTH * 8;
    uint32_t cell = 2 * SIM_SPI_HALF;
    uint32_t period = SIM_SPI_GAP + SIM_SPI_HALF + bits * cell + SIM_SPI_HALF;
    uint32_t t = index % period;

    if(t < SIM_SPI_GAP) //Idle: CS high, clock and MOSI low
        return 1u << 2;

    t -= SIM_SPI_GAP;

    uint32_t clk = 0;
    uint32_t mosi = 0;

    if(t < SIM_SPI_HALF) //CS asserted, first bit set up before the first edge
        mosi = spi_bit(0);
    else if(t < SIM_SPI_HALF + bits * cell)
    {
        uint32_t u = t - SIM_SPI_HALF;
        clk = (u % cell) >= SIM_SPI_HALF ? 1 : 0;
        mosi = spi_bit(u / cell);
    }

    return clk | (mosi << 1);
}

static uint32_t i2c_bit(uint32_t cell)
{
    uint32_t byteIndex = cell / 9;
    uint32_t position = cell % 9;

    if(position == 8) //ACK from the target
        return 0;

    uint8_t value = byteIndex == 0 ? (uint8_t)(SIM_I2C_ADDRESS << 1) : message[byteIndex - 1];
    return (value >> (7 - position)) & 1;
}

//I2C write: bit 0 = SCL, bit 1 = SDA
static uint32_t i2c_levels(uint32_t index)
{
    uint32_t cell = 4 * SIM_I2C_QUARTER;
    uint32_t cells = 9 * (MESSAGE_LENGTH + 1);
    uint32_t period = SIM_I2C_GAP + cell + cells * cell + cell;
    uint32_t t = index % period;

    if(t < SIM_I2C_GAP) //Idle: both lines high
        return 3;

    t -= SIM_I2C_GAP;

    if(t < cell) //Start condition: SDA falls while SCL is high
        return t < 2 * SIM_I2C_QUARTER ? 3 : 1;

    t -= cell;

    if(t < cells * cell)
    {
        uint32_t current = t / cell;
        uint32_t phase = t % cell;
        uint32_t previous = current == 0 ? 0 : i2c_bit(current - 1);

        //SCL low for two quarters (SDA changes after the first one), then high for two
        uint32_t scl = phase >= 2 * SIM_I2C_QUARTER ? 1 : 0;
        uint32_t sda = phase < SIM_I2C_QUARTER ? previous : i2c_bit(current);
        return scl | (sda << 1);
    }

    //Stop condition: SDA rises while SCL is high
    uint32_t phase = t - cells * cell;
    uint32_t scl = phase >= 2 * SIM_I2C_QUARTER ? 1 : 0;
    uint32_t sda = phase < 3 * SIM_I2C_QUARTER ? 0 : 1;
    return scl | (sda << 1);
}

uint32_t simulation_sample(uint8_t pattern, uint8_t channelCount, uint32_t index)
{
    uint32_t value = 0;

    if(channelCount > 32)
        channelCount = 32;

    switch(pattern)
    {
        case SIM_PATTERN_WALKING_ONE:

            if(channelCount > 0)
                value = 1u << ((index / SIM_WALK_STEP) % channelCount);

            break;

        case SIM_PATTERN_PROTOCOLS:
        {
            uint32_t spi = spi_levels(index);
            uint32_t i2c = i2c_levels(index);

            value = uart_level(index)
                  | ((spi & 1) << 1)            //SPI CLK
                  | (((spi >> 1) & 1) << 2)     //SPI MOSI
                  | (((spi >> 2) & 1) << 3)     //SPI CS
                  | ((i2c & 1) << 4)            //I2C SCL
                  | (((i2c >> 1) & 1) << 5);    //I2C SDA

            for(uint32_t channel = 6; channel < channelCount; channel++)
                value |= ((index >> ((channel - 6) % 16)) & 1u) << channel;

            break;
        }

        default: //SIM_PATTERN_COUNTER

            for(uint32_t channel = 0; channel < channelCount; channel++)
                value |= ((index >> (channel % 16)) & 1u) << channel;

            break;
    }

    if(channelCount < 32)
        value &= (1u << channelCount) - 1;

    return value;
}
