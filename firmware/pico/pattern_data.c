/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "pattern_data.h"
#include <string.h>

uint8_t gen_data_width(uint8_t flags)
{
    switch((flags & GEN_LOAD_WIDTH_MASK) >> GEN_LOAD_WIDTH_SHIFT)
    {
        case 0:
            return 1;
        case 1:
            return 2;
        case 2:
            return 4;
        default:
            return 0;
    }
}

uint8_t gen_data_width_for_pins(uint8_t pins)
{
    return pins <= 8 ? 1 : (pins <= 16 ? 2 : 4);
}

GEN_DATA_RESULT gen_data_load(uint8_t* buffer, uint32_t capacity, uint32_t offset, uint8_t flags, const uint8_t* data, uint32_t length, uint32_t* end)
{
    uint8_t width = gen_data_width(flags);

    if(width == 0 || (flags & ~GEN_LOAD_FLAGS_KNOWN))
        return GEN_DATA_FORMAT;

    uint64_t samples = 0;

    if(flags & GEN_LOAD_RUNS)
    {
        uint32_t pair = 2u + width;

        if(length % pair)
            return GEN_DATA_FORMAT;

        //Check everything before a byte is written
        for(uint32_t position = 0; position < length; position += pair)
        {
            uint16_t count = (uint16_t)(data[position] | (data[position + 1] << 8));

            if(count == 0)
                return GEN_DATA_FORMAT;

            samples += count;
        }
    }
    else
    {
        if(length % width)
            return GEN_DATA_FORMAT;

        samples = length / width;
    }

    uint64_t last = (uint64_t)offset + samples;

    if(last * width > capacity)
        return GEN_DATA_FULL;

    uint8_t* target = buffer + (uint64_t)offset * width;

    if(flags & GEN_LOAD_RUNS)
    {
        uint32_t pair = 2u + width;

        for(uint32_t position = 0; position < length; position += pair)
        {
            uint16_t count = (uint16_t)(data[position] | (data[position + 1] << 8));
            const uint8_t* sample = data + position + 2;

            if(width == 1)
            {
                memset(target, sample[0], count);
                target += count;
            }
            else
            {
                for(uint16_t i = 0; i < count; i++)
                {
                    memcpy(target, sample, width);
                    target += width;
                }
            }
        }
    }
    else
        memcpy(target, data, length);

    *end = (uint32_t)last;
    return GEN_DATA_OK;
}
