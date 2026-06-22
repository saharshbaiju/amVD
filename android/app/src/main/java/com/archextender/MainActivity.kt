package com.archextender

import android.animation.ObjectAnimator
import android.content.Context
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.SurfaceHolder
import android.view.View
import android.view.WindowManager
import android.view.animation.DecelerateInterpolator
import android.view.inputmethod.InputMethodManager
import androidx.appcompat.app.AppCompatActivity
import com.archextender.databinding.ActivityMainBinding

/**
 * Fullscreen client whose chrome never covers the video.
 *
 *  - Idle  -> a full "connect" screen (no video to obscure anyway).
 *  - Live  -> the video is full-bleed. The only persistent element is a slim
 *             edge handle hugging the right border; a status toast appears on
 *             change then dissolves; all real controls live in a drawer that
 *             slides in on demand and closes again.
 *
 * Auto-reconnect and the low-latency [StreamSession] decode path are unchanged.
 */
class MainActivity : AppCompatActivity(), SurfaceHolder.Callback, StreamSession.Listener {

    private lateinit var b: ActivityMainBinding
    private val ui = Handler(Looper.getMainLooper())
    private val prefs by lazy { getSharedPreferences("asext", Context.MODE_PRIVATE) }

    private var session: StreamSession? = null
    private var surfaceReady = false
    private var wantConnected = false
    private var reconnectScheduled = false
    private var drawerOpen = false

    private var host = "127.0.0.1"
    private var port = 5000
    private var streamInfo = ""
    private var serverConfig: ServerConfig? = null
    private var state = State.IDLE

    private var dotPulse: ObjectAnimator? = null
    private val autoHide = Runnable { fadeChrome() }

    private enum class State { IDLE, CONNECTING, CONNECTED, RECONNECTING }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityMainBinding.inflate(layoutInflater)
        setContentView(b.root)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        b.host.setText(prefs.getString("host", "127.0.0.1"))
        b.port.setText(prefs.getString("port", "5000"))

        b.surface.holder.addCallback(this)
        b.connectBtn.setOnClickListener { startConnect() }
        b.disconnectBtn.setOnClickListener { closeDrawer(); userDisconnect() }
        b.edgeHandle.setOnClickListener { openDrawer() }
        b.scrim.setOnClickListener { closeDrawer() }
        b.surface.setOnClickListener {
            if (drawerOpen) closeDrawer() else if (wantConnected) peekChrome()
        }

        // park the drawer off-screen on the right until summoned
        b.controlDrawer.translationX = 1200f
        b.controlDrawer.post { b.controlDrawer.translationX = b.controlDrawer.width.toFloat() }

        applyState()

        // launch straight into a connection: am start ... --ez autoconnect true
        if (intent?.getBooleanExtra("autoconnect", false) == true) startConnect()
    }

    // ----------------------------------------------------------------- actions

    private fun startConnect() {
        host = b.host.text.toString().trim().ifEmpty { "127.0.0.1" }
        port = b.port.text.toString().trim().toIntOrNull() ?: 5000
        prefs.edit().putString("host", host).putString("port", port.toString()).apply()
        hideKeyboard()
        wantConnected = true
        launchSession()
    }

    private fun userDisconnect() {
        wantConnected = false
        ui.removeCallbacks(autoHide)
        reconnectScheduled = false
        session?.stop()
        session = null
        streamInfo = ""
        serverConfig = null
        setState(State.IDLE)
    }

    // -------------------------------------------------------------- session io

    private fun launchSession() {
        if (!surfaceReady || !wantConnected || session != null) return
        setState(State.CONNECTING)
        session = StreamSession(host, port, b.surface.holder.surface, this)
    }

    private fun scheduleReconnect() {
        if (reconnectScheduled || !wantConnected) return
        reconnectScheduled = true
        ui.postDelayed({ reconnectScheduled = false; launchSession() }, 1200)
    }

    override fun onConnected() = runOnUiThread { setState(State.CONNECTED) }

    override fun onStopped(reason: String?) = runOnUiThread {
        session = null
        if (wantConnected) {
            setState(State.RECONNECTING, reason)
            scheduleReconnect()
        } else {
            setState(State.IDLE)
        }
    }

    override fun onStreamInfo(width: Int, height: Int) = runOnUiThread {
        streamInfo = "$width × $height"
        // Re-shape the surface to the real stream aspect ratio so a resolution
        // change actually takes effect on screen (and tablets stop stretching).
        b.surface.setAspectRatio(width, height)
        if (wantConnected) setState(State.CONNECTED)
    }

    override fun onServerConfig(config: ServerConfig) = runOnUiThread {
        serverConfig = config
        fillDrawer()
    }

    // ----------------------------------------------------------- state machine

    private fun setState(s: State, reason: String? = null) {
        state = s
        if (s == State.IDLE && reason != null && reason.isNotBlank()) {
            b.errorText.text = reason.lowercase()
            b.errorText.visibility = View.VISIBLE
        }
        applyState()
    }

    private fun applyState() {
        when (state) {
            State.IDLE -> {
                showConnectScreen()
                hideChromeNow()
                closeDrawer()
                stopPulse()
                b.connectBtn.text = getString(R.string.connect_cta)
            }
            State.CONNECTING -> {
                hideConnectScreen()
                stopPulse()
                showToast("CONNECTING …", R.color.teal)
                showHandle()
            }
            State.CONNECTED -> {
                hideConnectScreen()
                fillDrawer()
                startPulse()
                val res = if (streamInfo.isNotEmpty()) "  $streamInfo" else ""
                showToast("● LIVE$res", R.color.amber)
                showHandle()
                scheduleAutoHide()
            }
            State.RECONNECTING -> {
                hideConnectScreen()
                stopPulse()
                showToast("RECONNECTING …", R.color.warn)
                showHandle()
            }
        }
    }

    // ------------------------------------------------------------ connect view

    private fun showConnectScreen() {
        if (b.connectScreen.visibility == View.VISIBLE) return
        b.connectScreen.alpha = 0f
        b.connectScreen.visibility = View.VISIBLE
        b.connectScreen.animate().alpha(1f).setDuration(220).start()
    }

    private fun hideConnectScreen() {
        b.errorText.visibility = View.GONE
        if (b.connectScreen.visibility != View.VISIBLE) return
        b.connectScreen.animate().alpha(0f).setDuration(200)
            .withEndAction { b.connectScreen.visibility = View.GONE; enterImmersive() }
            .start()
    }

    // -------------------------------------------------------------- hud chrome

    private fun showToast(text: String, colorRes: Int) {
        b.statusToast.text = text
        b.statusToast.setTextColor(getColor(colorRes))
        appear(b.statusToast)
    }

    private fun showHandle() = appear(b.edgeHandle, 0.5f)

    private fun appear(v: View, target: Float = 1f) {
        if (v.visibility != View.VISIBLE) { v.visibility = View.VISIBLE; v.alpha = 0f }
        v.animate().alpha(target).setDuration(180).start()
    }

    private fun scheduleAutoHide() {
        ui.removeCallbacks(autoHide)
        if (state == State.CONNECTED && !drawerOpen) ui.postDelayed(autoHide, 3200)
    }

    /** Dissolve the toast and dim the handle so the video is unobstructed. */
    private fun fadeChrome() {
        b.statusToast.animate().alpha(0f).setDuration(450)
            .withEndAction { b.statusToast.visibility = View.GONE }.start()
        b.edgeHandle.animate().alpha(0.16f).setDuration(450).start()
    }

    private fun hideChromeNow() {
        ui.removeCallbacks(autoHide)
        b.statusToast.visibility = View.GONE
        b.edgeHandle.visibility = View.GONE
    }

    /** Bring chrome back briefly when the user taps the video. */
    private fun peekChrome() {
        val res = if (streamInfo.isNotEmpty()) "  $streamInfo" else ""
        when (state) {
            State.CONNECTED -> showToast("● LIVE$res", R.color.amber)
            State.RECONNECTING -> showToast("RECONNECTING …", R.color.warn)
            else -> showToast("CONNECTING …", R.color.teal)
        }
        showHandle()
        scheduleAutoHide()
    }

    // ------------------------------------------------------------------ drawer

    private fun openDrawer() {
        if (drawerOpen) return
        drawerOpen = true
        ui.removeCallbacks(autoHide)
        fillDrawer()
        b.scrim.alpha = 0f
        b.scrim.visibility = View.VISIBLE
        b.scrim.animate().alpha(1f).setDuration(200).start()
        b.controlDrawer.animate().translationX(0f).setDuration(260)
            .setInterpolator(DecelerateInterpolator()).start()
        b.edgeHandle.animate().alpha(0f).setDuration(160).start()
    }

    private fun closeDrawer() {
        if (!drawerOpen) {
            b.scrim.visibility = View.GONE
            return
        }
        drawerOpen = false
        b.scrim.animate().alpha(0f).setDuration(200)
            .withEndAction { b.scrim.visibility = View.GONE }.start()
        b.controlDrawer.animate().translationX(b.controlDrawer.width.toFloat())
            .setDuration(240).setInterpolator(DecelerateInterpolator()).start()
        if (state == State.CONNECTED) { showHandle(); scheduleAutoHide() }
    }

    private fun fillDrawer() {
        b.drawerState.text = when (state) {
            State.CONNECTED -> "LIVE"
            State.RECONNECTING -> "RECONNECT"
            State.CONNECTING -> "CONNECTING"
            State.IDLE -> "OFFLINE"
        }
        b.drawerRes.text = streamInfo.ifEmpty { "—" }
        b.drawerHost.text = "$host:$port"
        // 192.168.42.x is Android's USB-tethering subnet (the wired path, no USB
        // debugging); anything else is treated as a Wi-Fi LAN address.
        b.drawerLink.text = if (host.startsWith("192.168.42."))
            "USB · tethering" else "Wi-Fi · LAN"

        // Linux-side settings, advertised by the daemon's banner on connect.
        val sc = serverConfig
        b.drawerSide.text = sc?.side?.uppercase()?.ifEmpty { "—" } ?: "—"
        b.drawerFps.text = sc?.takeIf { it.fps > 0 }?.let { "${it.fps} Hz" } ?: "—"
        b.drawerScale.text = sc?.takeIf { it.scale > 0 }?.let { fmtScale(it.scale) } ?: "—"
        b.drawerQuality.text = sc?.takeIf { it.quality.isNotEmpty() }?.let {
            val br = if (it.bitrate.isNotEmpty()) " · ${it.bitrate}" else ""
            "${it.quality.uppercase()}$br"
        } ?: "—"
        b.drawerCodec.text = sc?.codec?.uppercase()?.ifEmpty { "—" } ?: "—"
    }

    /** "1.0" -> "1×", "1.25" -> "1.25×" (drop a trailing ".0"). */
    private fun fmtScale(scale: Double): String {
        val s = if (scale == scale.toLong().toDouble())
            scale.toLong().toString() else scale.toString()
        return "$s×"
    }

    // --------------------------------------------------------------- live dot

    private fun startPulse() {
        if (dotPulse?.isRunning == true) return
        b.dot.alpha = 1f
        dotPulse = ObjectAnimator.ofFloat(b.dot, "alpha", 1f, 0.25f).apply {
            duration = 850
            repeatCount = ObjectAnimator.INFINITE
            repeatMode = ObjectAnimator.REVERSE
            start()
        }
    }

    private fun stopPulse() {
        dotPulse?.cancel()
        dotPulse = null
        b.dot.alpha = 1f
    }

    // ------------------------------------------------------------------- misc

    private fun hideKeyboard() {
        val imm = getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager
        imm.hideSoftInputFromWindow(b.host.windowToken, 0)
    }

    // --- SurfaceHolder.Callback ---

    override fun surfaceCreated(holder: SurfaceHolder) {
        surfaceReady = true
        if (wantConnected) launchSession()
    }

    override fun surfaceChanged(holder: SurfaceHolder, f: Int, w: Int, h: Int) {}

    override fun surfaceDestroyed(holder: SurfaceHolder) {
        surfaceReady = false
        session?.stop()
        session = null
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
        wantConnected = false
        ui.removeCallbacksAndMessages(null)
        stopPulse()
        session?.stop()
    }
}
