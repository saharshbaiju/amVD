package com.archextender

import android.content.SharedPreferences
import android.net.Uri

/**
 * Where to connect: every address the computer advertised, plus its port and
 * name. Comes from the pairing QR shown by the Linux TUI:
 *
 *     asext://c?h=192.168.42.129,10.0.0.5&p=5000&n=archbox&v=1
 *
 * [StreamSession] tries all [hosts] in parallel, so the same QR works over USB
 * tethering and Wi-Fi. Scanning it with the stock camera also opens the app
 * (the manifest registers the `asext` scheme).
 */
data class ConnectTarget(val hosts: List<String>, val port: Int, val name: String) {

    /** "192.168.42.129 · 10.0.0.5" for display. */
    val addressLine: String get() = hosts.joinToString("  ·  ")

    val title: String get() = name.ifBlank { hosts.firstOrNull() ?: "computer" }

    fun save(p: SharedPreferences) {
        p.edit()
            .putString(KEY_HOSTS, hosts.joinToString(","))
            .putInt(KEY_PORT, port)
            .putString(KEY_NAME, name)
            .apply()
    }

    companion object {
        const val SCHEME = "asext"
        const val DEFAULT_PORT = 5000
        private const val KEY_HOSTS = "target_hosts"
        private const val KEY_PORT = "target_port"
        private const val KEY_NAME = "target_name"

        fun load(p: SharedPreferences): ConnectTarget? {
            val hosts = p.getString(KEY_HOSTS, null)?.split(',')
                ?.map { it.trim() }?.filter { it.isNotEmpty() }.orEmpty()
            if (hosts.isEmpty()) return null
            return ConnectTarget(hosts, p.getInt(KEY_PORT, DEFAULT_PORT),
                p.getString(KEY_NAME, "") ?: "")
        }

        /** Parse a scanned QR / deep link. Returns null if it isn't ours. */
        fun parse(raw: String?): ConnectTarget? {
            if (raw.isNullOrBlank()) return null
            val uri = try { Uri.parse(raw.trim()) } catch (_: Exception) { return null }
            if (uri.scheme != SCHEME) return null
            val hosts = uri.getQueryParameter("h")?.split(',')
                ?.map { it.trim() }?.filter { isHost(it) }.orEmpty()
            if (hosts.isEmpty()) return null
            val port = uri.getQueryParameter("p")?.toIntOrNull()?.takeIf { it in 1..65535 }
                ?: DEFAULT_PORT
            return ConnectTarget(hosts, port, uri.getQueryParameter("n").orEmpty())
        }

        /** Manual entry: "10.0.0.5", "10.0.0.5:5000" or a hostname. */
        fun manual(address: String, portText: String): ConnectTarget? {
            var host = address.trim()
            var port = portText.trim().toIntOrNull()
            val colon = host.lastIndexOf(':')
            if (colon > 0 && host.indexOf(':') == colon) {      // not IPv6
                port = host.substring(colon + 1).toIntOrNull() ?: port
                host = host.substring(0, colon)
            }
            if (!isHost(host)) return null
            return ConnectTarget(listOf(host), port?.takeIf { it in 1..65535 }
                ?: DEFAULT_PORT, "")
        }

        private fun isHost(s: String) =
            s.isNotEmpty() && s.length < 254 && s.all { it.isLetterOrDigit() || it in ".-:" }
    }
}
