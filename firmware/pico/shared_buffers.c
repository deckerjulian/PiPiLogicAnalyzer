/*
 * Copyright (C) Agustín Giménez Bernad (gusmanb), original LogicAnalyzer
 * Copyright (C) 2026 Julian Decker
 *
 * Part of openSciLab, based on his LogicAnalyzer firmware;
 * the changes are described in firmware/README.md.
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "board_settings.h"
#ifdef USE_CYGW_WIFI
    #include "shared_buffers.h"
    #include "structs.h"
    #include "event_machine.h"


    volatile WIFI_SETTINGS wifiSettings;
    EVENT_MACHINE wifiToFrontend;
    EVENT_MACHINE frontendToWifi;
#endif