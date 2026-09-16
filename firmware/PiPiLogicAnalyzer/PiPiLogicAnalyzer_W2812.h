/*
 * Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
 * Copyright (C) 2026 Julian Decker
 *
 * Part of LogicAnalyzer 7, based on his LogicAnalyzer firmware;
 * the changes are described in firmware/README.md.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "PiPiLogicAnalyzer_Board_Settings.h"

#ifdef WS2812_LED

    #ifndef __LOGICANALYZER_W2812__

        #define __LOGICANALYZER_W2812__

        void send_rgb(uint8_t r, uint8_t g, uint8_t b);
        void init_rgb();

    #endif

#endif