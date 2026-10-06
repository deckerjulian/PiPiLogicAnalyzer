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

    #ifndef __OPENSCILAB_WIFI__
        #define __OPENSCILAB_WIFI__


        typedef enum
        {
            VALIDATE_SETTINGS,
            WAITING_SETTINGS,
            CONNECTING_AP,
            STARTING_TCP_SERVER,
            WAITING_TCP_CLIENT,
            TCP_CLIENT_CONNECTED

        } WIFI_STATE_MACHINE;

        void runWiFiCore();
    #endif

#endif