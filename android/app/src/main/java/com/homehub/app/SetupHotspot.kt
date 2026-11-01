package com.homehub.app

import android.Manifest
import android.app.Activity
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiManager
import android.net.wifi.WifiNetworkSpecifier
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.SocketTimeoutException

/**
 * What the page can't do while adding a Xiaomi Wi-Fi device: a new device sets up its own open hotspot
 * (xiaomi-<model>_miapXXXX / _mibtXXXX), and only the phone can join it. The page drives the protocol
 * (through the hub) and uses this to join the hotspot and move UDP packets in and out of it.
 *
 * The hotspot has no internet, so the phone keeps using mobile data (or home Wi-Fi if it can hold both)
 * for the page's own requests; only the socket used here is bound to the hotspot.
 */
class SetupHotspot(private val activity: Activity, private val reply: (id: String, json: JSONObject) -> Unit) {
    private val cm = activity.getSystemService(ConnectivityManager::class.java)
    private var callback: ConnectivityManager.NetworkCallback? = null
    @Volatile private var network: Network? = null
    private var pendingScan: String? = null

    /** Setup hotspots in range: [{ssid, model, level}], strongest first. Needs the location permission. */
    fun scan(id: String) {
        if (activity.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED) {
            pendingScan = id
            activity.requestPermissions(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION), REQ_LOCATION)
            return
        }
        val wm = activity.applicationContext.getSystemService(WifiManager::class.java)
        @Suppress("DEPRECATION") wm.startScan()   // best effort; results below may be a few seconds old
        Thread {
            Thread.sleep(2500)
            val list = JSONArray()
            wm.scanResults.filter { SETUP_SSID.containsMatchIn(it.SSID) }.sortedByDescending { it.level }.forEach {
                list.put(JSONObject().put("ssid", it.SSID).put("level", it.level)
                    .put("model", it.SSID.removePrefix("xiaomi-").substringBefore("_mi").replace('-', '.')))
            }
            reply(id, JSONObject().put("ok", true).put("list", list))
        }.start()
    }

    fun onPermissionResult(granted: Boolean) {
        val id = pendingScan ?: return
        pendingScan = null
        if (granted) scan(id) else reply(id, JSONObject().put("ok", false).put("error", "需要允许「位置信息」权限才能找到附近设备的热点"))
    }

    /** Join the hotspot (Android shows its own "connect to device?" prompt once). */
    fun join(id: String, ssid: String) {
        leave()
        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .setNetworkSpecifier(WifiNetworkSpecifier.Builder().setSsid(ssid).build())
            .build()
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(n: Network) {
                network = n
                reply(id, JSONObject().put("ok", true).put("gateway", gatewayOf(n) ?: ""))
            }
            override fun onUnavailable() { reply(id, JSONObject().put("ok", false).put("error", "没连上设备的热点（取消了或超时）")) }
            override fun onLost(n: Network) { if (network == n) network = null }
        }
        callback = cb
        cm.requestNetwork(request, cb, JOIN_TIMEOUT_MS)
    }

    private fun gatewayOf(n: Network): String? =
        cm.getLinkProperties(n)?.routes?.firstOrNull { it.isDefaultRoute && it.gateway != null }?.gateway?.hostAddress

    /** Send one UDP packet (hex) to the device on the hotspot and return its answer (hex). */
    fun udp(id: String, host: String, port: Int, hex: String, timeoutMs: Int) {
        val n = network
        if (n == null) { reply(id, JSONObject().put("ok", false).put("error", "还没连上设备的热点")); return }
        Thread {
            try {
                DatagramSocket().use { s ->
                    n.bindSocket(s)
                    s.soTimeout = timeoutMs
                    val out = hexToBytes(hex)
                    s.send(DatagramPacket(out, out.size, InetAddress.getByName(host), port))
                    val buf = ByteArray(2048)
                    val p = DatagramPacket(buf, buf.size)
                    s.receive(p)
                    reply(id, JSONObject().put("ok", true).put("hex", buf.copyOf(p.length).joinToString("") { "%02x".format(it) }))
                }
            } catch (e: SocketTimeoutException) {
                reply(id, JSONObject().put("ok", false).put("timeout", true).put("error", "设备没有回应"))
            } catch (e: Exception) {
                reply(id, JSONObject().put("ok", false).put("error", e.message ?: e.javaClass.simpleName))
            }
        }.start()
    }

    /** Let go of the hotspot; the phone goes back to its normal Wi-Fi by itself. */
    fun leave() {
        callback?.let { runCatching { cm.unregisterNetworkCallback(it) } }
        callback = null
        network = null
    }

    private fun hexToBytes(h: String) = ByteArray(h.length / 2) { h.substring(it * 2, it * 2 + 2).toInt(16).toByte() }

    companion object {
        const val REQ_LOCATION = 4711
        private const val JOIN_TIMEOUT_MS = 30_000
        private val SETUP_SSID = Regex("_mi(ap|bt)[0-9a-fA-F]{4}$")
    }
}
