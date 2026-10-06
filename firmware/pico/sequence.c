/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab; the trigger sequences are described in firmware/README.md.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "sequence.h"

#include <string.h>

//On the device the evaluation runs from RAM and always optimised (the W builds are debug builds),
//so its speed and the reported SEQUENCE_MAX_RATE do not depend on the flash cache or the build type
#if defined(PICO_ON_DEVICE) && PICO_ON_DEVICE
    #include "pico.h"
    #define SEQ_FAST(name) __attribute__((optimize("O3"))) __not_in_flash_func(name)
#else
    #define SEQ_FAST(name) name
#endif

//Deadline when nothing but a change of a watched bit matters
#define SEQ_FAR (1u << 30)

//a lies before b (sample numbers compared in 32 bits)
#define BEFORE(a, b) ((int32_t)((uint32_t)(a) - (uint32_t)(b)) < 0)

bool seq_validate(const SEQ_STAGE* stages, uint8_t stageCount, uint8_t sampleBits)
{
    if(stageCount > SEQ_MAX_STAGES)
        return false;

    uint32_t bits = sampleBits >= 32 ? 0xFFFFFFFFu : ((1u << sampleBits) - 1);

    for(uint8_t i = 0; i < stageCount; i++)
    {
        const SEQ_STAGE* stage = &stages[i];

        if(stage->count < 1 || stage->count > SEQ_MAX_SAMPLES)
            return false;

        if(stage->withinSamples != SEQ_NO_LIMIT && stage->withinSamples > SEQ_MAX_SAMPLES)
            return false;

        switch(stage->kind)
        {
            case SEQ_PATTERN:
                if((stage->mask & ~bits) || (stage->value & ~stage->mask))
                    return false;
                break;

            case SEQ_EDGE:
                if(stage->bit >= sampleBits || stage->edge > SEQ_ANY)
                    return false;
                break;

            case SEQ_PULSE:
                if(stage->bit >= sampleBits || stage->edge > SEQ_ANY)
                    return false;
                if(stage->minSamples > SEQ_MAX_SAMPLES)
                    return false;
                if(stage->maxSamples != SEQ_NO_LIMIT && (stage->maxSamples > SEQ_MAX_SAMPLES || stage->maxSamples < stage->minSamples))
                    return false;
                break;

            case SEQ_GAP:
                if(stage->bit >= sampleBits || stage->minSamples < 1 || stage->minSamples > SEQ_MAX_SAMPLES)
                    return false;
                break;

            default:
                return false;
        }
    }

    return true;
}

void seq_start(SEQ_STATE* state)
{
    //Edges of the pulse channels are recorded in every stage: a pulse may begin before its stage.
    //A gap only looks at its channel during its own stage, where the channel is watched.
    state->tracked = 0;
    state->pulseCount = 0;

    for(uint8_t i = 0; i < state->stageCount; i++)
    {
        SEQ_STAGE* stage = &state->stages[i];
        uint32_t bit = 1u << stage->bit;

        if(stage->kind != SEQ_PULSE)
            continue;

        if(!(state->tracked & bit))
        {
            state->tracked |= bit;
            state->pulseMask[state->pulseCount++] = bit;
        }

        for(uint8_t slot = 0; slot < state->pulseCount; slot++)
            if(state->pulseMask[slot] == bit)
                stage->slot = slot;
    }

    for(uint8_t i = 0; i < state->stageCount; i++)
    {
        SEQ_STAGE* stage = &state->stages[i];
        stage->watch = state->tracked | (stage->kind == SEQ_PATTERN ? stage->mask : 1u << stage->bit);
        //Stages that have to be evaluated without a change now and then
        stage->timed = stage->kind == SEQ_PATTERN || stage->kind == SEQ_GAP || (i > 0 && stage->withinSamples != SEQ_NO_LIMIT);
    }

    state->stage = 0;
    state->occurrences = 0;
    state->stageStart = 0;
    state->firstSample = 0;
    state->gapStart = 0;
    state->pulseSeen = 0;
    memset(state->pulseEdge, 0, sizeof(state->pulseEdge));
    state->watch = state->stageCount ? state->stages[0].watch : 0;
    state->reference = 0;
    state->deadline = 0; //The first sample is always evaluated
    state->position = 0;
    state->ringIndex = 0;
    state->triggerSample = 0;
}

//Next sample that has to be evaluated without a change of a watched bit
static inline __attribute__((always_inline)) uint32_t next_deadline(const SEQ_STATE* state, const SEQ_STAGE* stage, uint32_t t)
{
    uint32_t deadline = t + SEQ_FAR;

    if(!stage->timed)
        return deadline;

    //A pattern that already matches when the stage starts counts as an occurrence
    if(stage->kind == SEQ_PATTERN && BEFORE(t, state->firstSample))
        deadline = state->firstSample;

    if(state->stage > 0 && stage->withinSamples != SEQ_NO_LIMIT)
    {
        uint32_t timeout = state->stageStart + stage->withinSamples + 1;
        if(BEFORE(timeout, deadline))
            deadline = timeout;
    }

    if(stage->kind == SEQ_GAP)
    {
        uint32_t end = state->gapStart + stage->minSamples;
        if(BEFORE(end, deadline))
            deadline = end;
    }

    if(!BEFORE(t, deadline))
        deadline = t + 1;

    return deadline;
}

//Evaluates sample t (value v, previous sample p); returns true when the sequence completed
static inline __attribute__((always_inline)) bool seq_sample(SEQ_STATE* state, uint32_t t, uint32_t v, uint32_t p)
{
    uint32_t changed = v ^ p;
    SEQ_STAGE* stage = &state->stages[state->stage];

    //The stage did not complete in time: start again with the first stage at this sample
    if(state->stage > 0 && stage->withinSamples != SEQ_NO_LIMIT && t - state->stageStart > stage->withinSamples)
    {
        state->stage = 0;
        state->occurrences = 0;
        state->stageStart = t;
        state->firstSample = t;
        state->gapStart = t;
        stage = &state->stages[0];
    }

    bool occurred = false;
    uint32_t bit = 1u << stage->bit;

    switch(stage->kind)
    {
        case SEQ_PATTERN:
            if((v & stage->mask) == stage->value)
                occurred = t == state->firstSample || (p & stage->mask) != stage->value;
            break;

        case SEQ_EDGE:
            if(changed & bit)
                occurred = stage->edge == SEQ_ANY || ((v & bit) != 0) == (stage->edge == SEQ_RISING);
            break;

        case SEQ_PULSE:
            //A high pulse ends with a falling edge, a low pulse with a rising one
            if((changed & bit) && (state->pulseSeen >> stage->slot & 1)
                && (stage->edge == SEQ_ANY || ((v & bit) == 0) == (stage->edge == SEQ_RISING)))
            {
                uint32_t width = t - state->pulseEdge[stage->slot];
                occurred = width >= stage->minSamples && (stage->maxSamples == SEQ_NO_LIMIT || width <= stage->maxSamples);
            }
            break;

        case SEQ_GAP:
            //The quiet time starts at the stage start, at an edge and after an occurrence
            if(changed & bit)
                state->gapStart = t;
            else if(t - state->gapStart >= stage->minSamples)
            {
                occurred = true;
                state->gapStart = t;
            }
            break;
    }

    if(occurred && ++state->occurrences >= stage->count)
    {
        if(++state->stage >= state->stageCount)
            return true;

        state->occurrences = 0;
        state->stageStart = t;
        state->firstSample = t + 1;
        state->gapStart = t;
        stage = &state->stages[state->stage];
    }

    //Edges of the pulse channels
    uint32_t edges = changed & state->tracked;
    if(edges)
    {
        uint32_t seen = state->pulseSeen;
        for(uint32_t slot = 0; slot < state->pulseCount; slot++)
        {
            if(edges & state->pulseMask[slot])
            {
                state->pulseEdge[slot] = t;
                seen |= 1u << slot;
            }
        }
        state->pulseSeen = seen;
    }

    state->watch = stage->watch;
    state->deadline = next_deadline(state, stage, t);
    return false;
}

//Evaluates the samples of one contiguous part of the ring buffer; the sample width is a constant
//of every specialisation, so the skipping loop is a load, a mask and a compare per sample
#define SEQ_SCAN_IMPL(TYPE) \
    const TYPE* samples = (const TYPE*)ring; \
    uint32_t index = state->ringIndex; \
    uint32_t stop = index + count; \
    uint32_t t = (uint32_t)state->position; \
    bool hasPrevious = state->position > 0; /* a run never wraps: index 0 is its first sample */ \
    while(index < stop) \
    { \
        uint32_t watch = state->watch; \
        uint32_t reference = state->reference; \
        uint32_t limit = stop; \
        uint32_t room = state->deadline - t; \
        if(room < stop - index) \
            limit = index + room; \
        uint32_t next = index; \
        while(next < limit && (samples[next] & watch) == reference) \
            next++; \
        t += next - index; \
        index = next; \
        if(index >= stop) \
            break; \
        uint32_t value = samples[index]; \
        uint32_t previous; \
        if(index > 0) \
            previous = samples[index - 1]; \
        else if(hasPrevious) \
            previous = samples[ringSamples - 1]; \
        else \
            previous = value; \
        if(seq_sample(state, t, value, previous)) \
        { \
            state->triggerSample = state->position + (index - state->ringIndex); \
            state->position = state->triggerSample + 1; \
            state->ringIndex = index + 1 >= ringSamples ? 0 : index + 1; \
            return true; \
        } \
        state->reference = value & state->watch; \
        index++; \
        t++; \
    }

static bool SEQ_FAST(seq_scan8)(SEQ_STATE* state, const void* ring, uint32_t ringSamples, uint32_t count)
{
    SEQ_SCAN_IMPL(uint8_t)
    return false;
}

static bool SEQ_FAST(seq_scan16)(SEQ_STATE* state, const void* ring, uint32_t ringSamples, uint32_t count)
{
    SEQ_SCAN_IMPL(uint16_t)
    return false;
}

static bool SEQ_FAST(seq_scan32)(SEQ_STATE* state, const void* ring, uint32_t ringSamples, uint32_t count)
{
    SEQ_SCAN_IMPL(uint32_t)
    return false;
}

bool SEQ_FAST(seq_scan)(SEQ_STATE* state, const void* ring, uint32_t ringSamples, uint8_t bytesPerSample, uint64_t end)
{
    //Without stages the first sample triggers
    if(state->stageCount == 0)
    {
        if(state->position >= end)
            return false;

        state->triggerSample = state->position++;
        return true;
    }

    while(state->position < end)
    {
        //Up to the end of the ring buffer or of the available samples
        uint32_t count = ringSamples - state->ringIndex;
        if(end - state->position < count)
            count = (uint32_t)(end - state->position);

        bool done;
        switch(bytesPerSample)
        {
            case 1:
                done = seq_scan8(state, ring, ringSamples, count);
                break;
            case 2:
                done = seq_scan16(state, ring, ringSamples, count);
                break;
            default:
                done = seq_scan32(state, ring, ringSamples, count);
                break;
        }

        if(done)
            return true;

        state->position += count;
        state->ringIndex += count;
        if(state->ringIndex >= ringSamples)
            state->ringIndex = 0;
    }

    return false;
}
