/*
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "proto.h"

#define FRAME_START_0 0x55
#define FRAME_START_1 0xAA
#define FRAME_END_0 0xAA
#define FRAME_END_1 0x55
#define ESCAPE 0xF0

void proto_reset(PROTO_PARSER* parser)
{
    parser->position = 0;
}

PROTO_EVENT proto_feed(PROTO_PARSER* parser, uint8_t byte, const uint8_t** payload, uint16_t* length)
{
    //Outside a frame: wait for the start condition, 0xFF stops a capture
    if(parser->position == 0)
    {
        if(byte == FRAME_START_0)
            parser->buffer[parser->position++] = byte;
        else if(byte == PROTO_CANCEL_BYTE)
            return PROTO_CANCEL;

        return PROTO_NONE;
    }

    if(parser->position == 1)
    {
        if(byte == FRAME_START_1)
            parser->buffer[parser->position++] = byte;
        else if(byte == FRAME_START_0)
            parser->position = 1; //0x55 0x55 0xAA: the second 0x55 may start the frame
        else
        {
            parser->position = 0;
            if(byte == PROTO_CANCEL_BYTE)
                return PROTO_CANCEL;
        }

        return PROTO_NONE;
    }

    if(parser->position >= PROTO_FRAME_MAX)
    {
        parser->position = 0;
        return PROTO_OVERFLOW;
    }

    parser->buffer[parser->position++] = byte;

    //Escaped payload bytes are never 0xAA or 0x55, so the first 0xAA 0x55 ends the frame
    if(parser->position < 4 || parser->buffer[parser->position - 2] != FRAME_END_0 || byte != FRAME_END_1)
        return PROTO_NONE;

    //Unescape the payload in place, behind the start condition
    uint16_t end = parser->position - 2;
    uint16_t destination = 0;

    for(uint16_t source = 2; source < end; source++)
    {
        uint8_t value = parser->buffer[source];

        if(value == ESCAPE && source + 1 < end)
            value = parser->buffer[++source] ^ ESCAPE;

        parser->buffer[destination++] = value;
    }

    parser->position = 0;
    *payload = parser->buffer;
    *length = destination;
    return PROTO_FRAME;
}

uint16_t proto_build(const uint8_t* payload, uint16_t length, uint8_t* frame, uint16_t size)
{
    uint16_t used = 0;

    if(size < 4)
        return 0;

    frame[used++] = FRAME_START_0;
    frame[used++] = FRAME_START_1;

    for(uint16_t i = 0; i < length; i++)
    {
        uint8_t value = payload[i];
        bool escape = value == FRAME_START_0 || value == FRAME_START_1 || value == ESCAPE;

        if(used + (escape ? 2 : 1) + 2 > size)
            return 0;

        if(escape)
        {
            frame[used++] = ESCAPE;
            frame[used++] = value ^ ESCAPE;
        }
        else
            frame[used++] = value;
    }

    frame[used++] = FRAME_END_0;
    frame[used++] = FRAME_END_1;
    return used;
}

bool proto_allowed_while_capturing(uint8_t command)
{
    return (command >= CMD_WRITE && command <= CMD_SAFE) || command == CMD_GEN_STOP || command == CMD_GEN_STATUS;
}
