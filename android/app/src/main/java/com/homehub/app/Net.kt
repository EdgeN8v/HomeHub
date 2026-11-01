package com.homehub.app

import android.util.Base64
import kotlinx.coroutines.Dispatchers
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

    @Volatile var base: String? = null
        private set
    private var checkedAt = 0L

    /** Where requests go right now, without waiting for a probe: the last route known to work. */
    fun current(): String = base ?: BuildConfig.LANURL

    /** Start from the route that worked last time (saved by the activity). */
    fun assume(url: String) {
        if (base == null) base = url
    }

    /**
     * The LAN address when it answers within 400 ms (at home), else the public one if that answers.
     * When neither does (no network at all) the previous route is kept rather than flipping to one
     * that is just as dead. Cached for a minute.
     */
    suspend fun baseUrl(force: Boolean = false): String = withContext(Dispatchers.IO) {
        val now = System.currentTimeMillis()
        if (!force) base?.takeIf { now - checkedAt < 60_000 }?.let { return@withContext it }
        val chosen = when {
            reachable(BuildConfig.LANURL, 400) -> BuildConfig.LANURL
            reachable(BuildConfig.PUBLICURL, 2500) -> BuildConfig.PUBLICURL
            else -> base ?: BuildConfig.LANURL
        }
        base = chosen
        checkedAt = now
        chosen
    }

    fun routeName(): String = when (base) {
        null -> "未连接"
        BuildConfig.LANURL -> "家里直连"
        else -> "公网"
    }

    private fun reachable(url: String, timeoutMs: Int): Boolean = try {
        val uri = URI(url)
        Socket().use { it.connect(InetSocketAddress(uri.host, if (uri.port > 0) uri.port else 443), timeoutMs) }
        true
    } catch (e: Exception) {
        false
    }
}
