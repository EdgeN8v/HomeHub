package com.homehub.idea

import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.net.ConnectivityManager
import android.net.Network
import android.net.Uri
import android.os.Bundle
import android.util.Base64
import android.webkit.JavascriptInterface
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import kotlinx.coroutines.Job
import kotlinx.coroutines.MainScope
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.ByteArrayInputStream
import java.io.File
import java.security.MessageDigest

/**
 * A thin shell around the hub's Idea page (/idea). The page lives on the server, so most changes reach the phone
 * without an app update. The page is loaded from a made-up origin (https://idea.local); every request to it is relayed
 * here to the hub over the pinned client with the Idea token, on whichever address answers (home / Pi domain / NAS domain).
 * The page and its script are kept on the phone too, so the app opens - with the last data - even with no connection.
 */
class MainActivity : Activity() {
    private val scope = MainScope()
    private lateinit var web: WebView
    private var downloadJob: Job? = null
    private val pageDir by lazy { File(filesDir, "page").apply { mkdirs() } }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Net.init(this)
        web = WebView(this)
        setContentView(web)
        web.setBackgroundColor(resources.getColor(R.color.bg, theme))
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.settings.textZoom = 100
        web.addJavascriptInterface(Bridge(), "IdeaApp")
        web.webViewClient = Client()
        // Without a WebChromeClient the WebView drops confirm()/alert() silently (confirm() returns false) - show them.
        web.webChromeClient = object : android.webkit.WebChromeClient() {
            override fun onJsAlert(view: WebView, url: String?, message: String?, result: android.webkit.JsResult): Boolean {
                AlertDialog.Builder(this@MainActivity).setMessage(message).setPositiveButton("知道了") { _, _ -> result.confirm() }
                    .setOnCancelListener { result.cancel() }.show()
                return true
            }
            override fun onJsConfirm(view: WebView, url: String?, message: String?, result: android.webkit.JsResult): Boolean {
                AlertDialog.Builder(this@MainActivity).setMessage(message).setPositiveButton("确定") { _, _ -> result.confirm() }
                    .setNegativeButton("取消") { _, _ -> result.cancel() }.setOnCancelListener { result.cancel() }.show()
                return true
            }
        }
        val prefs = getSharedPreferences("app", MODE_PRIVATE)
        if (prefs.getInt("last_version", 0) != Updater.versionCode(this)) {
            web.clearCache(true)
            prefs.edit().putInt("last_version", Updater.versionCode(this)).apply()
        }
        web.loadUrl("$ORIGIN/")
        scope.launch {
            Net.baseUrl(force = true)
            checkUpdate(silent = true)
        }
        getSystemService(ConnectivityManager::class.java).registerDefaultNetworkCallback(object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                runOnUiThread { scope.launch { Net.baseUrl(force = true); web.evaluateJavascript("window.load && load()", null) } }
            }
        })
    }

    override fun onResume() {
        super.onResume()
        if (::web.isInitialized) web.evaluateJavascript("window.load && load()", null)
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else @Suppress("DEPRECATION") super.onBackPressed()
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    // ---- relay ---------------------------------------------------------------------------------

    private fun keyOf(path: String) = File(pageDir, MessageDigest.getInstance("SHA-1").digest(path.toByteArray()).joinToString("") { "%02x".format(it) })

    private fun relay(request: WebResourceRequest): WebResourceResponse {
        val url = request.url
        var path = (url.encodedPath ?: "/").let { if (it == "/" || it == "") "/idea" else it } + (url.encodedQuery?.let { "?$it" } ?: "")
        val body = when (request.method) {
            "GET" -> null
            "POST" -> request.requestHeaders.entries.firstOrNull { it.key.equals("X-HH-Body", true) }?.value
                ?.let { String(Base64.decode(it, Base64.DEFAULT)) } ?: return status(400, "POST body missing")
            else -> return status(405, "method not relayed")
        }
        val isPage = request.method == "GET" && !path.startsWith("/api/")
        for (attempt in 0..1) {
            val base = if (attempt == 0) Net.current() else runBlocking { Net.failover() }
            try {
                val b = Request.Builder().url(base + path).header("Authorization", "Bearer ${BuildConfig.TOKEN}")
                if (body != null) b.post(body.toRequestBody("application/json".toMediaType()))
                Net.client.newCall(b.build()).execute().use { r ->
                    val type = r.header("Content-Type") ?: "application/octet-stream"
                    val bytes = r.body?.bytes() ?: ByteArray(0)
                    if (isPage && r.isSuccessful) keyOf(path.substringBefore('?')).writeBytes(type.toByteArray() + "\n".toByteArray() + bytes)
                    return WebResourceResponse(type.substringBefore(';').trim(), charset(type), r.code, r.message.ifBlank { "OK" },
                        mapOf("Cache-Control" to "no-cache"), ByteArrayInputStream(bytes))
                }
            } catch (e: Exception) {
                if (attempt == 1 || Net.current() == base) break
            }
        }
        // nobody answers: the page and its script from the phone's last copy (the page shows its last data itself)
        if (isPage) keyOf(path.substringBefore('?')).takeIf { it.exists() }?.readBytes()?.let { all ->
            val nl = all.indexOf('\n'.code.toByte())
            val type = String(all, 0, nl)
            return WebResourceResponse(type.substringBefore(';').trim(), charset(type), ByteArrayInputStream(all.copyOfRange(nl + 1, all.size)))
        }
        if (path == "/idea") return WebResourceResponse("text/html", "utf-8", ByteArrayInputStream(OFFLINE.toByteArray()))
        return status(503, "home unreachable")
    }

    private fun charset(type: String) = Regex("charset=([\\w-]+)").find(type)?.groupValues?.get(1) ?: "utf-8"
    private fun status(code: Int, text: String) =
        WebResourceResponse("text/plain", "utf-8", code, text, mapOf("Cache-Control" to "no-cache"), ByteArrayInputStream(text.toByteArray()))

    private inner class Client : WebViewClient() {
        override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? =
            if (request.url.host == HOST) relay(request) else null

        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            if (request.url.host == HOST) return false
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(request.url.toString())))
            return true
        }
    }

    // ---- updates -------------------------------------------------------------------------------

    private suspend fun checkUpdate(silent: Boolean) {
        val info = try { Updater.check(this) } catch (e: Exception) { if (!silent) toast("检查更新失败：${e.message}"); return }
        if (info == null) { if (!silent) toast("已经是最新版 ${Updater.versionName(this)}"); return }
        val size = if (info.sizeBytes > 0) "（%.1f MB）".format(info.sizeBytes / 1_048_576.0) else ""
        AlertDialog.Builder(this).setTitle("Idea 有新版本 ${info.versionName}$size").setMessage(info.notes.ifBlank { "更新到新版本" })
            .setPositiveButton("更新") { _, _ -> startUpdate(info) }.setNegativeButton("以后", null).show()
    }

    private fun startUpdate(info: UpdateInfo) {
        val dialog = AlertDialog.Builder(this).setTitle("正在下载 ${info.versionName}").setMessage("0%").setCancelable(false)
            .setNegativeButton("取消") { _, _ -> downloadJob?.cancel() }.show()
        downloadJob = scope.launch {
            try {
                val file = Updater.download(this@MainActivity, info) { p -> runOnUiThread { dialog.setMessage("${(p * 100).toInt()}%") } }
                dialog.dismiss()
                if (!Updater.install(this@MainActivity, file)) toast("请允许 Idea 安装应用，然后再点一次更新")
            } catch (e: Exception) {
                dialog.dismiss()
                if (e !is kotlinx.coroutines.CancellationException) toast("下载失败：${e.message}")
            }
        }
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_LONG).show()

    /** window.IdeaApp in the page. */
    private inner class Bridge {
        @JavascriptInterface fun versionName(): String = Updater.versionName(this@MainActivity)
        @JavascriptInterface fun route(): String = Net.routeName()
        @JavascriptInterface fun checkUpdate() = runOnUiThread { scope.launch { checkUpdate(silent = false) } }
    }

    companion object {
        const val HOST = "idea.local"
        const val ORIGIN = "https://$HOST"
        private val OFFLINE = """
            <meta name=viewport content="width=device-width,initial-scale=1">
            <body style="margin:0;min-height:100vh;display:grid;place-items:center;font:16px sans-serif;background:#f6f6f3;color:#333">
            <div style="text-align:center;padding:24px"><div style="font-size:48px">💡</div>第一次打开需要连上家里的服务器<br>
            <small style="color:#888">家里 Wi-Fi、或者外网（pi / nas 域名）都可以，连上后自动进入</small></div>
            <script>setTimeout(() => location.reload(), 4000)</script>
        """.trimIndent()
    }
}
