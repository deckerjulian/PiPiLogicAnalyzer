/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of PiPiLogicAnalyzer; the trigger sequences are described in firmware/README.md.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef __LOGICANALYZER_SEQUENCE__
#define __LOGICANALYZER_SEQUENCE__

//Evaluation of trigger sequences (trigger type 7) on the samples in the capture ring buffer.
//
//Plain C without Pico SDK dependencies, so it can be compiled on a PC and checked against the
//reference implementation of the tests (tests/test_pico_triggers.py).
//
//Every time value is a number of samples (the host converts nanoseconds with the sample rate).
//A stage is evaluated from the sample after the one that completed the previous stage; the
//sample that completes the last stage is the trigger sample.

#include <stdint.h>
#include <stdbool.h>

//Most stages a sequence may have (reported as TRIGGER_SEQUENCE=<n>)
#define SEQ_MAX_STAGES 8
//"No limit" of maxSamples and withinSamples
#define SEQ_NO_LIMIT 0xFFFFFFFFu
//Longest time a stage may use: differences of sample numbers are computed in 32 bits
#define SEQ_MAX_SAMPLES 0x7FFFFFFFu

typedef enum
{
    SEQ_PATTERN = 0,    //The bits of mask have the levels of value
    SEQ_EDGE = 1,       //An edge on bit
    SEQ_PULSE = 2,      //A pulse on bit with a width of minSamples..maxSamples, recognised at its end
    SEQ_GAP = 3         //No edge on bit for minSamples samples (timeout)

} SEQ_KIND;

typedef enum
{
    SEQ_RISING = 0,     //EDGE: rising edge, PULSE: high pulse
    SEQ_FALLING = 1,    //EDGE: falling edge, PULSE: low pulse
    SEQ_ANY = 2         //Either

} SEQ_EDGE_KIND;

typedef struct _SEQ_STAGE
{
    uint8_t kind;               //SEQ_KIND
    uint8_t edge;               //SEQ_EDGE_KIND (EDGE and PULSE)
    uint8_t bit;                //Sample bit of the channel (EDGE, PULSE and GAP)
    uint32_t mask;              //PATTERN: sample bits compared
    uint32_t value;             //PATTERN: their levels
    uint32_t minSamples;        //PULSE: shortest width, GAP: length of the gap
    uint32_t maxSamples;        //PULSE: longest width (SEQ_NO_LIMIT: none)
    uint32_t count;             //Occurrences completing the stage (at least 1)
    uint32_t withinSamples;     //Samples after the previous stage within which the stage must complete,
                                //else the sequence starts again (SEQ_NO_LIMIT: none, ignored for the first stage)

    //Set by seq_start
    uint32_t watch;             //Bits whose changes the stage has to look at
    uint8_t slot;               //PULSE: slot of its channel in pulseEdge
    bool timed;                 //Evaluated at deadlines as well (pattern, gap, time limit)

} SEQ_STAGE;

typedef struct _SEQ_STATE
{
    //Progress; the fields used for every sample come first (short load offsets on the Cortex-M0+)
    uint32_t watch;                         //Watched bits of the current stage
    uint32_t reference;                     //Watched bits of the sample before position
    uint32_t deadline;                      //Sample that has to be evaluated even without a change
    uint32_t stageStart;                    //Sample that completed the previous stage (time reference)
    uint32_t firstSample;                   //First sample the current stage evaluates
    uint32_t gapStart;                      //GAP: start of the quiet time (stage start, last edge or occurrence)
    uint32_t occurrences;                   //Occurrences of the condition of the current stage so far
    uint32_t tracked;                       //Bits of the PULSE stages: their edges are recorded in every stage
    uint32_t pulseSeen;                     //Slots with an edge so far (bit n = slot n)
    uint32_t pulseEdge[SEQ_MAX_STAGES];     //Sample of the last edge of every slot
    uint32_t pulseMask[SEQ_MAX_STAGES];     //Sample bit of every slot
    uint8_t pulseCount;                     //Slots in use
    uint8_t stage;                          //Current stage
    uint8_t stageCount;                     //Stages of the sequence (set before seq_start)

    uint64_t position;                      //Next sample to evaluate (counted from the start of the capture)
    uint32_t ringIndex;                     //Its index in the ring buffer
    uint64_t triggerSample;                 //Sample that completed the sequence (valid once seq_scan returned true)

    SEQ_STAGE stages[SEQ_MAX_STAGES];       //Set before seq_start

} SEQ_STATE;

/// @brief Checks the stages (kinds, edges, counts and times) and the bits against the sample width
/// @param stages Stages to check
/// @param stageCount Number of stages
/// @param sampleBits Bits of a sample (8, 16 or 32)
/// @return True if the sequence can be evaluated
bool seq_validate(const SEQ_STAGE* stages, uint8_t stageCount, uint8_t sampleBits);

/// @brief Prepares the evaluation from the first sample of a capture on (stages and stageCount set)
void seq_start(SEQ_STATE* state);

/// @brief Evaluates the samples from state->position up to end - 1
/// @param state Evaluation state
/// @param ring Ring buffer of the samples (sample n at index n % ringSamples)
/// @param ringSamples Samples in the ring buffer
/// @param bytesPerSample 1, 2 or 4
/// @param end First sample not available yet
/// @return True once the sequence completed: state->triggerSample is the trigger sample
bool seq_scan(SEQ_STATE* state, const void* ring, uint32_t ringSamples, uint8_t bytesPerSample, uint64_t end);

#endif
