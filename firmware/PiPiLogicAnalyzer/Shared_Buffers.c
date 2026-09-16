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
#ifdef USE_CYGW_WIFI
    #include "Shared_Buffers.h"
    #include "PiPiLogicAnalyzer_Structs.h"
    #include "Event_Machine.h"


    volatile WIFI_SETTINGS wifiSettings;
    EVENT_MACHINE wifiToFrontend;
    EVENT_MACHINE frontendToWifi;
#endif