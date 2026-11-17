package com.homehub.app

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * The home server is reached by a domain that only has an IPv6 address, so a
 * network with no IPv6 route (a Wi-Fi at the office or a café, or a mobile
 * plan without IPv6) can't reach it — and which of Wi-Fi / mobile data is the
 * broken one changes with the place. When neither server address answers on
 * the system's default network, [bindToOther] pins this app's own traffic to
 * the other radio (Wi-Fi <-> cellular) so a retry can succeed; [release] puts
 * it back. Nothing else on the phone is touched. Same trick as the ledger app.
 */
object NetFallback {
    @Volatile private var appContext: Context? = null
    @Volatile private var boundTransport: Int? = null
    private var callback: ConnectivityManager.NetworkCallback? = null

    fun init(context: Context) {
        appContext = context.applicationContext
    }

    /** Blocks up to ~6 s; call off the main thread. True once this app's traffic is pinned to the radio the system is not using by default. */
    @Synchronized
    fun bindToOther(): Boolean {
        val context = appContext ?: return false
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val want = when (currentTransport(cm)) {
            NetworkCapabilities.TRANSPORT_WIFI -> NetworkCapabilities.TRANSPORT_CELLULAR
            else -> NetworkCapabilities.TRANSPORT_WIFI
        }
        if (boundTransport == want) return true
        // No such network at all (airplane mode, no SIM): nothing to wait 6 s for.
        val exists = cm.allNetworks.any { network ->
            cm.getNetworkCapabilities(network)?.let {
                it.hasTransport(want) && it.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            } == true
        }
        if (!exists) return false

        release()
        val latch = CountDownLatch(1)
        var got: Network? = null
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                got = network
                cm.bindProcessToNetwork(network)
                boundTransport = want
                latch.countDown()
            }

            override fun onLost(network: Network) {
                if (got == network) {
                    boundTransport = null
                    cm.bindProcessToNetwork(null)
                }
            }
        }
        return try {
            cm.requestNetwork(
                NetworkRequest.Builder()
                    .addTransportType(want)
                    .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                    .build(),
                cb
            )
            callback = cb
            val ok = latch.await(6, TimeUnit.SECONDS) && boundTransport == want
            if (!ok) release()
            ok
        } catch (e: Exception) {
            false
        }
    }

    /** Back to the system's default network. */
    @Synchronized
    fun release() {
        val context = appContext ?: return
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        try {
            callback?.let { cm.unregisterNetworkCallback(it) }
        } catch (e: Exception) {
            // already gone
        }
        callback = null
        if (boundTransport != null) cm.bindProcessToNetwork(null)
        boundTransport = null
    }

    private fun currentTransport(cm: ConnectivityManager): Int? {
        val caps = cm.getNetworkCapabilities(cm.activeNetwork) ?: return null
        return when {
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> NetworkCapabilities.TRANSPORT_WIFI
            caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> NetworkCapabilities.TRANSPORT_CELLULAR
            else -> null
        }
    }
}
