// Copyright (C) 2026 Julian Decker
// Part of openSciLab. SPDX-License-Identifier: GPL-3.0-or-later

package org.openscilab.bridge

import android.app.Activity
import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import org.openscilab.bridge.core.BridgeServer
import org.openscilab.bridge.core.SnapshotCache
import java.util.Locale
import java.util.concurrent.Executors

/** Start, stop and the state of the bridge. */
class MainActivity : Activity() {
    private lateinit var status: TextView
    private val handler = Handler(Looper.getMainLooper())
    private val worker = Executors.newSingleThreadExecutor()
    private val refresh = object : Runnable {
        override fun run() {
            update()
            handler.postDelayed(this, 1000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val padding = (16 * resources.displayMetrics.density).toInt()
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(padding, padding, padding, padding)
        }
        val buttons = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        buttons.addView(Button(this).apply {
            text = "Start"
            setOnClickListener { BridgeService.start(this@MainActivity); update() }
        })
        buttons.addView(Button(this).apply {
            text = "Stop"
            setOnClickListener { BridgeService.stop(this@MainActivity); update() }
        })
        status = TextView(this).apply {
            typeface = Typeface.MONOSPACE
            textSize = 16f
            setPadding(0, padding, 0, 0)
        }
        layout.addView(TextView(this).apply { text = getString(R.string.app_name); textSize = 22f })
        layout.addView(buttons)
        layout.addView(status)
        setContentView(ScrollView(this).apply { addView(layout) })
    }

    override fun onResume() {
        super.onResume()
        handler.post(refresh)
    }

    override fun onPause() {
        handler.removeCallbacks(refresh)
        super.onPause()
    }

    override fun onDestroy() {
        worker.shutdown()
        super.onDestroy()
    }

    /** Collects the state off the main thread (the cache size walks the files). */
    private fun update() {
        val root = BridgeService.cacheRoot(this)
        worker.execute {
            val text = describe(BridgeService.server, root)
            handler.post { status.text = text }
        }
    }

    private fun describe(server: BridgeServer?, root: java.io.File): String {
        val lines = ArrayList<String>()
        lines += "Version ${BuildConfig.VERSION_NAME}, protocol ${org.openscilab.bridge.core.BRIDGE_PROTOCOL}"
        if (server == null) {
            lines += "Bridge: stopped"
            BridgeService.startError?.let { lines += "Error: $it" }
        } else {
            val state = server.status()
            lines += "Bridge: listening on TCP ${state.port}, beacon on UDP 5561"
            lines += "Addresses: ${BridgeServer.localAddresses().joinToString(", ").ifEmpty { "none" }}"
            lines += "Instrument 127.0.0.1:5555: ${if (state.instrumentConnected) "connected" else "not connected"}"
            lines += "Model ${state.model}, serial ${state.serial}"
            lines += "Clients: ${state.clients}"
            lines += "Beacons sent: ${state.beacons}"
            state.lastBench?.let { lines += String.format(Locale.ROOT, "Last bench: %.1f MB/s", it / 1e6) }
            state.lastError?.let { lines += "Last error: $it" }
        }
        // Not a new SnapshotCache while the server runs: that would remove a snapshot being written.
        val cache = server?.handler?.cache ?: SnapshotCache(root)
        val snapshots = cache.list()
        lines += String.format(
            Locale.ROOT, "Cache: %d snapshots, %.1f of %.1f MB", snapshots.size,
            snapshots.sumOf { it.bytes() } / 1e6, cache.limit / 1e6,
        )
        lines += "Cache folder: $root"
        return lines.joinToString("\n")
    }
}
