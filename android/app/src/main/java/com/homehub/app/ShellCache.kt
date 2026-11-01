package com.homehub.app

import android.content.Context
import android.net.Uri
import android.util.Base64
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.ByteArrayInputStream
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Everything the page asks of home goes through here, over the pinned OkHttp client, so the WebView
 * itself never talks TLS to the hub (it would reject the self-signed certificate on fetch/XHR).
 *
 * Page files (/, /washer, /static/...) are kept on the phone and served cache-first, so the app opens
 * at once and also without network - like Mijia, whose screens are part of the app; the page then
 * shows its cached device snapshot and reconnects by itself. Versioned assets (?v=<hash>) never change;
 * pages and unversioned files are refetched in the background, and [pageChanged] tells the activity to
 * reload once a deploy changed them. Cache keys ignore the host, so LAN and public share one copy.
 *
 * WebView doesn't expose request bodies to an interceptor, so the page sends a POST's JSON body
 * again, base64'd, in the X-HH-Body header (app.js api()). A POST without it is not forwarded.
 */
class ShellCache(context: Context) {
    private val dir = File(context.filesDir, "shell").apply { mkdirs() }
    private val background = Executors.newSingleThreadExecutor()
    private val streamClient by lazy { Net.client.newBuilder().readTimeout(0, TimeUnit.MILLISECONDS).build() }

    @Volatile var pageChanged = false

    private fun isShell(url: Uri): Boolean {
        val p = url.path ?: return false
        return p == "/" || p == "/washer" || p.startsWith("/static/")
    }

    /**
     * The response for a request the page made to [MainActivity.ORIGIN]: the same path on [base], the
     * hub's address on the route currently in use (LAN or public).
     */
    fun respond(request: WebResourceRequest, base: String): WebResourceResponse {
        val url = Uri.parse(base + (request.url.encodedPath ?: "/") + (request.url.encodedQuery?.let { "?$it" } ?: ""))
        return respondTo(url, request) ?: unavailable(url)
    }

    private fun respondTo(url: Uri, request: WebResourceRequest): WebResourceResponse? {
        if (request.method == "GET" && isShell(url)) {
            cached(url)?.let { (type, body) ->
                if (url.getQueryParameter("v") == null) background.execute { fetchShell(url) }
                return response(type, body)
            }
            // Not cached: fetch it; offline, fall back to the last copy of the same file in any version
            // so the page never comes up without its script or styles.
            val got = fetchShell(url) ?: cached(url.buildUpon().clearQuery().build())
            return got?.let { (type, body) -> response(type, body) }
        }
        return forward(url, request)
    }

    // ---- page files

    private fun key(url: Uri): String {
        val raw = (url.encodedPath ?: "/") + "?" + (url.encodedQuery ?: "")
        return MessageDigest.getInstance("SHA-1").digest(raw.toByteArray()).joinToString("") { "%02x".format(it) }
    }

    private fun cached(url: Uri): Pair<String, ByteArray>? {
        val k = key(url)
        val body = File(dir, "$k.body")
        val type = File(dir, "$k.type")
        if (!body.exists() || !type.exists()) return null
        return type.readText() to body.readBytes()
    }

    /** Fetches a page file and stores it; null when home can't be reached. */
    private fun fetchShell(url: Uri): Pair<String, ByteArray>? = try {
        val req = Request.Builder().url(url.toString()).header("Authorization", "Bearer ${BuildConfig.TOKEN}").build()
        Net.client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) return null
            val type = resp.header("Content-Type") ?: "application/octet-stream"
            val body = resp.body!!.bytes()
            val old = cached(url)?.second
            val isPage = type.startsWith("text/html")
            // A new page version references new ?v= assets: download those first, so the stored page is
            // always complete - offline it must not come up without its script or styles.
            if (isPage) prefetchAssets(url, String(body))
            store(url, type, body)
            if (url.getQueryParameter("v") != null) store(url.buildUpon().clearQuery().build(), type, body)   // "latest" copy
            if (old != null && !old.contentEquals(body) && url.getQueryParameter("v") == null) pageChanged = true
            type to body
        }
    } catch (e: Exception) {
        null
    }

    private fun store(url: Uri, type: String, body: ByteArray) {
        val k = key(url)
        File(dir, "$k.body.tmp").apply { writeBytes(body); renameTo(File(dir, "$k.body")) }
        File(dir, "$k.type").writeText(type)
    }

    private fun prefetchAssets(page: Uri, html: String) {
        for (m in Regex("""/static/[\w.-]+\?v=[0-9a-f]+""").findAll(html)) {
            val asset = page.buildUpon().encodedPath(m.value.substringBefore("?")).encodedQuery(m.value.substringAfter("?")).build()
            if (cached(asset) == null) fetchShell(asset)
        }
    }

    // ---- API calls and the live stream

    private fun forward(url: Uri, request: WebResourceRequest): WebResourceResponse {
        val headers = request.requestHeaders.mapKeys { it.key.lowercase() }
        val body = when (request.method) {
            "GET" -> null
            "POST" -> headers["x-hh-body"]?.let { String(Base64.decode(it, Base64.DEFAULT)) }
                ?: return status(400, "POST body missing (X-HH-Body)")   // never send a POST without its body
            else -> return status(405, "method not relayed")
        }
        val builder = Request.Builder().url(url.toString()).header("Authorization", "Bearer ${BuildConfig.TOKEN}")
        if (body != null) builder.post(body.toRequestBody("application/json".toMediaType()))
        val stream = url.path == "/api/stream"
        return try {
            val resp = (if (stream) streamClient else Net.client).newCall(builder.build()).execute()
            val type = resp.header("Content-Type") ?: "application/octet-stream"
            WebResourceResponse(
                type.substringBefore(";").trim(), charsetOf(type), resp.code, resp.message.ifBlank { "OK" },
                mapOf("Cache-Control" to "no-cache"), resp.body!!.byteStream(),
            )
        } catch (e: Exception) {
            status(503, "home unreachable")
        }
    }

    // ---- helpers

    private fun charsetOf(type: String) = Regex("charset=([\\w-]+)").find(type)?.groupValues?.get(1) ?: "utf-8"

    private fun response(type: String, body: ByteArray) =
        WebResourceResponse(type.substringBefore(";").trim(), charsetOf(type), ByteArrayInputStream(body))

    private fun status(code: Int, text: String) = WebResourceResponse(
        "text/plain", "utf-8", code, text, mapOf("Cache-Control" to "no-cache"), ByteArrayInputStream(text.toByteArray()),
    )

    /**
     * Nothing cached and home unreachable. For the page itself (only possible before the very first
     * successful start) a small page that keeps retrying; anything else fails fast so fetches don't hang.
     */
    private fun unavailable(url: Uri): WebResourceResponse {
        if (url.path != "/") return status(503, "home unreachable")
        val html = """
            <meta name=viewport content="width=device-width,initial-scale=1">
            <body style="margin:0;min-height:100vh;display:grid;place-items:center;font:16px sans-serif;color:#fff;
              background:linear-gradient(175deg,#7f86b5,#cfa9b1 55%,#f1e4dd)">
            <div style="text-align:center;padding:24px">第一次打开需要连上家里<br><small>连上后会自动进入</small></div>
            <script>setTimeout(() => location.reload(), 4000)</script>
        """.trimIndent()
        return WebResourceResponse("text/html", "utf-8", ByteArrayInputStream(html.toByteArray()))
    }
}
