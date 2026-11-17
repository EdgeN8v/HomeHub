package com.homehub.idea

import android.content.Context
import android.util.Base64
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager

/**
 * Reaching the hub from anywhere - the same scheme as the HomeHub and Ledger apps.
 *
 * HTTPS trusts exactly one public key, the hub's (its certificate is self-signed), so the same key answers on the
 * home IPv4 addresses and on the two public domains. Which address to use: all are probed at once and the first that
 * answers wins in order of priority - home first (1 s window), then the domains. The list is not fixed in the code:
 * the hub's address table (GET /api/endpoints, service "homehub") is fetched and kept; the built-in four are only
 * the first-start fallback. When nothing answers on the current network, this app's traffic is moved to the other
 * radio (Wi-Fi <-> mobile, [NetFallback]) and it tries again.
 */
object Net {
    val client: OkHttpClient by lazy {
        val trust = PinnedTrustManager()
        val ssl = SSLContext.getInstance("TLS").apply { init(null, arrayOf(trust), SecureRandom()) }
        OkHttpClient.Builder()
            .sslSocketFactory(ssl.socketFactory, trust)
            .hostnameVerifier { _, _ -> true }               // the pin proves the server; the name differs per route
            .connectTimeout(8, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .build()
    }

    private class PinnedTrustManager : X509TrustManager {
        override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) = throw CertificateException("not used")
        override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
            val leaf = chain?.firstOrNull() ?: throw CertificateException("服务器没有给证书")
            leaf.checkValidity()
            val pin = "sha256/" + Base64.encodeToString(MessageDigest.getInstance("SHA-256").digest(leaf.publicKey.encoded), Base64.NO_WRAP)
            if (pin != BuildConfig.PIN) throw CertificateException("证书指纹不对，可能不是家里的服务器")
        }
        override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
    }

    private class Table(val version: Int, val check: String, val expect: String, val lan: List<String>, val wan: List<String>)

    // Home: Orange Pi, then the NAS stand-in. Outside: the public domains - not written in the source (it is public on GitHub):
    // BuildConfig.WAN is a comma-separated list from idea_app/secrets.properties (gitignored). The address table the hub
    // sends (GET /api/endpoints) replaces both lists once the app has connected once.
    private val fallback = Table(0, "/static/icon.svg", "<svg",
        listOf("https://192.168.31.61:8200", "https://192.168.31.131:8095"),
        BuildConfig.WAN.split(',').map { it.trim().trimEnd('/') }.filter { it.isNotEmpty() })

    @Volatile private var table = fallback
    @Volatile var base: String? = null
        private set
    private var checkedAt = 0L
    private var lastFailover = 0L
    private var prefs: android.content.SharedPreferences? = null

    fun init(context: Context) {
        NetFallback.init(context)
        prefs = context.getSharedPreferences("net", Context.MODE_PRIVATE)
        prefs?.getString("endpoints", null)?.let { parse(it) }?.let { table = it }
        base = prefs?.getString("last_base", null)
    }

    private fun parse(text: String): Table? = try {
        val root = JSONObject(text)
        val svc = root.getJSONObject("services").getJSONObject("homehub")
        fun list(k: String) = svc.getJSONArray(k).let { a -> (0 until a.length()).map { a.getString(it).trimEnd('/') } }
        val lan = list("lan"); val wan = list("wan")
        if (lan.isEmpty() && wan.isEmpty()) null
        else Table(root.optInt("version"), svc.optString("check", "/static/icon.svg"), svc.optString("expect", ""),
            // the built-in addresses stay in as a last resort, behind the table's own
            (lan + fallback.lan).distinct(), (wan + fallback.wan).distinct())
    } catch (e: Exception) { null }

    /** Where requests go right now, without waiting for a probe. */
    fun current(): String = base ?: table.lan.first()

    fun routeName(): String = when {
        base == null -> "未连接"
        table.lan.contains(base) -> "家里直连"
        else -> "外网 " + (base!!.substringAfter("://").substringBefore(':'))
    }

    private fun probe(url: String, t: Table, timeoutMs: Long): Boolean = try {
        client.newBuilder().callTimeout(timeoutMs, TimeUnit.MILLISECONDS).build()
            .newCall(Request.Builder().url(url + t.check).build()).execute()
            .use { r -> r.isSuccessful && (t.expect.isEmpty() || (r.body?.string() ?: "").contains(t.expect)) }
    } catch (e: Exception) { false }

    suspend fun baseUrl(force: Boolean = false): String = withContext(Dispatchers.IO) {
        val now = System.currentTimeMillis()
        if (!force && base != null && now - checkedAt < 60_000) return@withContext base!!
        var chosen = pick()
        if (chosen == null && NetFallback.bindToOther()) chosen = pick()
        if (chosen != null) {
            base = chosen
            prefs?.edit()?.putString("last_base", chosen)?.apply()
            refreshTable(chosen)
        }
        checkedAt = now
        current()
    }

    private suspend fun pick(): String? = coroutineScope {
        val t = table
        val lan = t.lan.map { it to async { probe(it, t, 1500) } }
        val wan = t.wan.map { it to async { probe(it, t, 5000) } }
        val limit = System.currentTimeMillis() + 1000
        for ((url, job) in lan) {
            val left = limit - System.currentTimeMillis()
            val ok = if (left > 0) withTimeoutOrNull(left) { job.await() } == true else (job.isCompleted && job.await())
            if (ok) { (lan + wan).forEach { it.second.cancel() }; return@coroutineScope url }
        }
        for ((url, job) in wan) if (job.await()) { (lan + wan).forEach { it.second.cancel() }; return@coroutineScope url }
        null
    }

    private fun refreshTable(from: String) {
        try {
            val text = client.newBuilder().callTimeout(4, TimeUnit.SECONDS).build()
                .newCall(Request.Builder().url("$from/api/endpoints").build()).execute()
                .use { if (it.isSuccessful) it.body?.string() else null } ?: return
            val fresh = parse(text) ?: return
            if (fresh.version > table.version) {
                table = fresh
                prefs?.edit()?.putString("endpoints", text)?.apply()
            }
        } catch (e: Exception) { /* keep what we have */ }
    }

    /** A request just failed: probe again (at most every 5 s) and return the address that answers now. */
    suspend fun failover(): String {
        val now = System.currentTimeMillis()
        if (now - lastFailover < 5_000) return current()
        lastFailover = now
        return baseUrl(force = true)
    }
}
