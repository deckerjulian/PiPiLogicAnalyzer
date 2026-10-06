// Copyright (C) 2026 Julian Decker
// Part of openSciLab. SPDX-License-Identifier: GPL-3.0-or-later

package org.openscilab.bridge

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Starts the bridge after the oscilloscope boots, if it was running before, so the instrument
 * shows up in openSciLab without touching it.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED && BridgeService.enabled(context)) {
            BridgeService.start(context)
        }
    }
}
