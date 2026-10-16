package com.archextender

import android.content.SharedPreferences
import android.net.Uri

/**
 * Where to connect: every address the computer advertised, plus its port,
 * name and pairing key. Comes from the pairing QR shown by the Linux TUI:
 *
 *     asext://c?h=192.168.42.129,10.0.0.5&p=5000&n=archbox&k=<key>&v=1
 *
 * [StreamSession] tries all [hosts] in parallel, so the same QR works over USB
 * tethering and Wi-Fi. Scanning it with the stock camera also opens the app
 * (the manifest registers the `asext` scheme).
 *
 * [pairingKey] (`k`) is what lets this phone send touches: the computer only
 * accepts touch input from a phone that presents it. It is empty for an
 * address typed by hand (view only) or a QR from an older computer version.
 */
data class ConnectTarget(
    val hosts: List<String>,
    val port: Int,
    val name: String,
    val pairingKey: String = "",
) {

    /** "192.168.42.129 · 10.0.0.5" for display. */
    val addressLine: String get() = hosts.joinToString("  ·  ")

    val title: String get() = name.ifBlank { hosts.firstOrNull() ?: "computer" }

    fun save(p: SharedPreferences) {
        p.edit()
            .putString(KEY_HOSTS, hosts.joinToString(","))
            .putInt(KEY_PORT, port)
            .putString(KEY_NAME, name)
            .putString(KEY_PAIRING, pairingKey)
            .apply()
    }

    companion object {
        const val SCHEME = "asext"
        const val DEFAULT_PORT = 5000
        private const val KEY_HOSTS = "target_hosts"
        private const val KEY_PORT = "target_port"
        private const val KEY_NAME = "target_name"
        private const val KEY_PAIRING = "target_pairing_key"

        fun load(p: SharedPreferences): ConnectTarget? {
            val hosts = p.getString(KEY_HOSTS, null)?.split(',')
                ?.map { it.trim() }?.filter { it.isNotEmpty() }.orEmpty()
            if (hosts.isEmpty()) return null
            return ConnectTarget(hosts, p.getInt(KEY_PORT, DEFAULT_PORT),
                p.getString(KEY_NAME, "") ?: "", p.getString(KEY_PAIRING, "") ?: "")
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
            return ConnectTarget(hosts, port, uri.getQueryParameter("n").orEmpty(),
                uri.getQueryParameter("k").orEmpty())
        }

        /**
         * Manual entry: "10.0.0.5", "10.0.0.5:5000" or a hostname. Keeps the
         * name and pairing key of [previous] when it's the same computer (the
         * user only corrected the address it's reached on).
         */
        fun manual(address: String, portText: String, previous: ConnectTarget? = null): ConnectTarget? {
            var host = address.trim()
            var port = portText.trim().toIntOrNull()
            val colon = host.lastIndexOf(':')
            if (colon > 0 && host.indexOf(':') == colon) {      // not IPv6
                port = host.substring(colon + 1).toIntOrNull() ?: port
                host = host.substring(0, colon)
            }
            if (!isHost(host)) return null
            val validPort = port?.takeIf { it in 1..65535 } ?: DEFAULT_PORT
            val sameComputer = previous?.takeIf { host in it.hosts && validPort == it.port }
            return ConnectTarget(listOf(host), validPort,
                sameComputer?.name.orEmpty(), sameComputer?.pairingKey.orEmpty())
        }

        private fun isHost(s: String) =
            s.isNotEmpty() && s.length < 254 && s.all { it.isLetterOrDigit() || it in ".-:" }
    }
}
