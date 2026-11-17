package com.homehub.app

import android.util.Base64
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import java.net.InetSocketAddress
import java.net.Socket
import java.net.URI
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager

/**
 * HTTPS that trusts exactly one public key: the hub's.
 *
 * The hub's certificate is self-signed (tools/gen_secrets.py), so the CA check can't apply. Instead
 * the leaf's public-key hash must equal [BuildConfig.PIN]; anything else (a captive portal, a proxy
 * on public Wi-Fi) fails. The pin already proves identity, so the hostname check is skipped - the
 * same key answers on the LAN IP and on the public domain.
 */
object Net {
    val client: OkHttpClient by lazy {
        val trust = PinnedTrustManager()
        val ssl = SSLContext.getInstance("TLS").apply { init(null, arrayOf(trust), SecureRandom()) }
        OkHttpClient.Builder()
            .sslSocketFactory(ssl.socketFactory, trust)
            .hostnameVerifier { _, _ -> true }
            .connectTimeout(8, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            // OkHttp's default is 5 requests per host: a few held-open ones (the live /api/stream, a camera picture) made every other
            // call from the page - a washer job poll, a button press - wait in line for 10-20 s. Plenty of room instead.
            .dispatcher(okhttp3.Dispatcher().apply { maxRequests = 64; maxRequestsPerHost = 24 })
            .build()
    }

    fun pinOf(cert: X509Certificate): String =
        "sha256/" + Base64.encodeToString(MessageDigest.getInstance("SHA-256").digest(cert.publicKey.encoded), Base64.NO_WRAP)

    private class PinnedTrustManager : X509TrustManager {
        override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) =
            throw CertificateException("not used")

        override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
            val leaf = chain?.firstOrNull() ?: throw CertificateException("服务器没有给证书")
            leaf.checkValidity()
            if (pinOf(leaf) != BuildConfig.PIN) throw CertificateException("证书指纹不对，可能不是家里的服务器")
        }

        override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
    }

    // ---- which address to use ------------------------------------------------

    /** The public address chosen in the app (saved by the activity); null = the one built in. */
    @Volatile var publicOverride: String? = null

    fun publicUrl(): String = publicOverride ?: defaultPublic()

    private fun defaultPublic(): String = table.wan.firstOrNull() ?: BuildConfig.PUBLICURL

    /**
     * What the person typed (a domain, domain:port or a full https:// address) -> a clean https URL, or null when it is
     * not an address. Without a port the built-in one is kept (the router's port forward does not change with the name).
     */
    fun normalizePublic(input: String): String? {
        var t = input.trim().trimEnd('/')
        if (t.isEmpty()) return null
        if (!t.contains("://")) t = "https://$t"
        val uri = try { URI(t) } catch (e: Exception) { return null }
        val host = uri.host?.lowercase() ?: return null
        if (uri.scheme != "https" || !Regex("[a-z0-9]([a-z0-9.-]*[a-z0-9])?").matches(host)) return null
        val port = if (uri.port > 0) uri.port else URI(defaultPublic()).port.takeIf { it > 0 } ?: 443
        return if (port == 443) "https://$host" else "https://$host:$port"
    }

    /**
     * "Does this public address reach the hub?" - fetches the hub's public icon over the pinned HTTPS, so a yes also
     * means it is the right server. Returns {ok, msg}; msg says what is wrong in plain words.
     */
    fun testPublic(input: String): Map<String, Any> {
        val url = normalizePublic(input.ifBlank { defaultPublic() }) ?: return mapOf("ok" to false, "msg" to "这不是一个有效的地址，写成 域名 或 域名:端口")
        val t0 = System.currentTimeMillis()
        return try {
            val call = client.newBuilder().callTimeout(8, TimeUnit.SECONDS).build().newCall(okhttp3.Request.Builder().url("$url/static/icon.svg").build())
            call.execute().use { r ->
                if (r.isSuccessful) mapOf("ok" to true, "msg" to "连上了，家里的服务器在回应（${System.currentTimeMillis() - t0} 毫秒）")
                else mapOf("ok" to false, "msg" to "地址能打开，但回应不对（${r.code}），端口可能转发到别的设备上了")
            }
        } catch (e: java.net.UnknownHostException) {
            mapOf("ok" to false, "msg" to "这个域名解析不到，看看域名解析有没有生效")
        } catch (e: java.io.InterruptedIOException) {
            mapOf("ok" to false, "msg" to "超时了：端口没转发出来，或者家里的网络、NAS 没开")
        } catch (e: java.net.ConnectException) {
            mapOf("ok" to false, "msg" to "连不上：端口没有转发到香橙派")
        } catch (e: javax.net.ssl.SSLException) {
            mapOf("ok" to false, "msg" to "连到了，但不是家里的服务器（证书对不上）")
        } catch (e: Exception) {
            mapOf("ok" to false, "msg" to "失败：${e.message ?: e.javaClass.simpleName}")
        }
    }

    @Volatile var base: String? = null
        private set
    private var checkedAt = 0L

    // ---- the address table: tried in order, LAN first ------------------------------
    //
    // The addresses are NOT fixed in the code: the hub keeps the table (GET /api/endpoints), the app fetches it in the
    // background and keeps the last copy it got. What is written below is only the first-start fallback.

    private class Table(val version: Int, val check: String, val expect: String, val lan: List<String>, val wan: List<String>)

    private val fallbackTable = Table(0, "/static/icon.svg", "<svg",
        listOf(BuildConfig.LANURL, "https://192.168.31.131:8095"),
        listOf(BuildConfig.PUBLICURL))

    @Volatile private var table: Table = fallbackTable
    private var appContext: android.content.Context? = null

    /** Load the saved table (call once from the activity, before the first request). */
    fun init(context: android.content.Context) {
        appContext = context.applicationContext
        NetFallback.init(context)
        val saved = appContext!!.getSharedPreferences("app", android.content.Context.MODE_PRIVATE).getString("endpoints", null) ?: return
        parseTable(saved)?.let { table = it }
    }

    private fun parseTable(text: String): Table? = try {
        val root = org.json.JSONObject(text)
        val svc = root.getJSONObject("services").getJSONObject("homehub")
        fun list(k: String) = svc.getJSONArray(k).let { a -> (0 until a.length()).map { a.getString(it).trimEnd('/') } }
        val lan = list("lan"); val wan = list("wan")
        if (lan.isEmpty() && wan.isEmpty()) null
        else Table(root.optInt("version"), svc.optString("check", "/static/icon.svg"), svc.optString("expect", ""), lan, wan)
    } catch (e: Exception) { null }

    private fun lanList() = table.lan
    private fun wanList(): List<String> = listOfNotNull(publicOverride).plus(table.wan).distinct()

    /** Where requests go right now, without waiting for a probe: the last route known to work. */
    fun current(): String = base ?: lanList().firstOrNull() ?: BuildConfig.LANURL

    /** Start from the route that worked last time (saved by the activity). */
    fun assume(url: String) {
        if (base == null) base = url
    }

    /** Is this address the hub itself? The pinned certificate already proves the server; the answer must also be the right page. */
    private fun probe(url: String, t: Table, timeoutMs: Long): Boolean = try {
        val call = client.newBuilder().callTimeout(timeoutMs, TimeUnit.MILLISECONDS).build()
            .newCall(okhttp3.Request.Builder().url(url + t.check).build())
        call.execute().use { r -> r.isSuccessful && (t.expect.isEmpty() || (r.body?.string() ?: "").contains(t.expect)) }
    } catch (e: Exception) { false }

    /**
     * Probes every address at once and takes the first that answers in order of priority: the home addresses first (a short
     * window of 1 s - at home they answer at once), then the public ones. Not "whoever is fastest": at home the public
     * domain can answer first through the router and send traffic round the outside. When nothing answers on the current
     * network, this app's traffic is pinned to the other radio (Wi-Fi <-> mobile) and it tries again. When still nothing
     * (no network at all) the previous route is kept rather than flipping to one that is just as dead. Cached for a minute.
     */
    suspend fun baseUrl(force: Boolean = false): String = withContext(Dispatchers.IO) {
        val now = System.currentTimeMillis()
        if (!force) base?.takeIf { now - checkedAt < 60_000 }?.let { return@withContext it }
        var chosen = pick()
        if (chosen == null && NetFallback.bindToOther()) chosen = pick()
        if (chosen != null) {
            base = chosen
            refreshTable(chosen)
        }
        checkedAt = now
        base ?: current()
    }

    private suspend fun pick(): String? = kotlinx.coroutines.coroutineScope {
        val t = table
        val lan = lanList().map { it to async { probe(it, t, 1500) } }
        val wan = wanList().map { it to async { probe(it, t, 4000) } }
        val limit = System.currentTimeMillis() + 1000
        for ((url, job) in lan) {
            val left = limit - System.currentTimeMillis()
            val ok = if (left > 0) kotlinx.coroutines.withTimeoutOrNull(left) { job.await() } == true else (job.isCompleted && job.await())
            if (ok) { (lan + wan).forEach { it.second.cancel() }; return@coroutineScope url }
        }
        for ((url, job) in wan) if (job.await()) { (lan + wan).forEach { it.second.cancel() }; return@coroutineScope url }
        null
    }

    /** Fetch the table from the server that just answered; a newer one is kept for the next start and the next probe. */
    private fun refreshTable(from: String) {
        try {
            val text = client.newBuilder().callTimeout(4, TimeUnit.SECONDS).build()
                .newCall(okhttp3.Request.Builder().url("$from/api/endpoints").build()).execute().use { if (it.isSuccessful) it.body?.string() else null } ?: return
            val fresh = parseTable(text) ?: return
            if (fresh.version > table.version) {
                table = fresh
                appContext?.getSharedPreferences("app", android.content.Context.MODE_PRIVATE)?.edit()?.putString("endpoints", text)?.apply()
            }
        } catch (e: Exception) { /* keep the table we have */ }
    }

    /**
     * The sentinels (they decide which machine runs the hub) answer on :8210 of both machines with the hub's own certificate.
     * Their progress can be read with the hub token, so the phone can follow a switch of the main machine while no hub runs.
     */
    fun sentinelBases(): List<String> {
        fun lan(h: String) = h.startsWith("192.168.") || h.startsWith("10.") || Regex("^172[.](1[6-9]|2[0-9]|3[01])[.]").containsMatchIn(h)
        // Only from the home network: from outside the sentinels' port is not open, and trying them would only make every poll wait
        // for timeouts. From outside the progress comes through the hub's own public port (a stand-in answers there during a switch).
        val cur = runCatching { URI(current()).host }.getOrNull() ?: return emptyList()
        if (!lan(cur)) return emptyList()
        return (listOf(current()) + lanList()).mapNotNull { runCatching { URI(it).host }.getOrNull() }.filter(::lan).distinct().map { "https://$it:8210" }
    }

    private var lastFailover = 0L

    /**
     * A request just failed: probe again (at most every 5 s, so a dead network does not make every request wait for a probe)
     * and return the base that answers now - the hub may have moved to the other machine.
     */
    suspend fun failover(): String {
        val now = System.currentTimeMillis()
        if (now - lastFailover < 5_000) return current()
        lastFailover = now
        return baseUrl(force = true)
    }

    fun routeName(): String = when {
        base == null -> "未连接"
        lanList().contains(base) -> "家里直连"
        else -> "公网"
    }

}
