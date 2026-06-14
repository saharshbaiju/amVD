package com.archextender

import android.os.Bundle
import android.view.SurfaceHolder
import android.view.View
import android.view.WindowManager
import androidx.appcompat.app.AppCompatActivity
import com.archextender.databinding.ActivityMainBinding

/**
 * Single-screen base client: a fullscreen SurfaceView plus a small connection
 * overlay. Connect over USB by running `adb reverse tcp:5000 tcp:5000` on the PC
 * and using host 127.0.0.1; or over WiFi with the PC's LAN IP.
 */
class MainActivity : AppCompatActivity(), SurfaceHolder.Callback, StreamSession.Listener {

    private lateinit var b: ActivityMainBinding
    private var session: StreamSession? = null
    private var surfaceReady = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityMainBinding.inflate(layoutInflater)
        setContentView(b.root)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        b.surface.holder.addCallback(this)
        b.connect.setOnClickListener { toggleConnection() }

        // Tap the video to bring the overlay back while connected.
        b.surface.setOnClickListener {
            if (session != null) b.overlay.visibility = View.VISIBLE
        }
    }

    private fun toggleConnection() {
        if (session != null) {
            disconnect("disconnected")
            return
        }
        if (!surfaceReady) {
            setStatus("surface not ready")
            return
        }
        val host = b.host.text.toString().trim()
        val port = b.port.text.toString().trim().toIntOrNull() ?: 5000
        setStatus("connecting to $host:$port …")
        session = StreamSession(host, port, b.surface.holder.surface, this)
    }

    private fun disconnect(msg: String) {
        session?.stop()
        session?.join()
        session = null
        runOnUiThread {
            b.connect.text = getString(R.string.connect)
            b.overlay.visibility = View.VISIBLE
            setStatus(msg)
        }
    }

    private fun setStatus(s: String) = runOnUiThread { b.status.text = s }

    // --- StreamSession.Listener ---

    override fun onConnected() = runOnUiThread {
        b.connect.text = getString(R.string.disconnect)
        b.overlay.visibility = View.GONE
        setStatus("connected")
        enterImmersive()
    }

    override fun onStopped(reason: String?) {
        if (session != null) disconnect(reason ?: "stream ended")
    }

    // --- SurfaceHolder.Callback ---

    override fun surfaceCreated(holder: SurfaceHolder) { surfaceReady = true }
    override fun surfaceChanged(holder: SurfaceHolder, f: Int, w: Int, h: Int) {}
    override fun surfaceDestroyed(holder: SurfaceHolder) {
        surfaceReady = false
        disconnect("surface destroyed")
    }

    private fun enterImmersive() {
        window.decorView.systemUiVisibility = (
            View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                or View.SYSTEM_UI_FLAG_FULLSCREEN
                or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
            )
    }

    override fun onDestroy() {
        super.onDestroy()
        session?.stop()
    }
}
