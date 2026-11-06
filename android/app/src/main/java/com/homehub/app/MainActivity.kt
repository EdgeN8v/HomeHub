package com.homehub.app

import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.graphics.Color
import android.net.ConnectivityManager
import android.net.Network
import android.net.Uri
import android.os.Bundle
import android.webkit.JavascriptInterface
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import kotlinx.coroutines.Job
import kotlinx.coroutines.MainScope
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.io.File

/**
 * A thin native shell around the hub's web page. The page itself lives on the server, so most
 * changes reach the phone without an app update; the shell adds what a browser can't: trusting the
 * hub's self-signed cert by pin, carrying the token, picking LAN vs public, and self-updating.
 */
class MainActivity : Activity() {
    private val scope = MainScope()
    private val shell by lazy { ShellCache(this) }
    private lateinit var web: WebView
    private var pendingInstall: File? = null
    private var downloadJob: Job? = null
    // answers go back to the page as window.hhSetup(id, json) (see Bridge.setup)
    private val hotspot by lazy {
        SetupHotspot(this) { id, json ->
            runOnUiThread { web.evaluateJavascript("window.hhSetup && hhSetup(${JSONObject.quote(id)}, ${json})", null) }
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        web = WebView(this)
        setContentView(web)
        edgeToEdge()
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        // WebView scales page text with the system font size (about 1.2x on this phone), which made
        // names overflow the cards. Native apps like Mijia don't; the page sizes its own text.
        web.settings.textZoom = 100
        web.addJavascriptInterface(Bridge(), "HomeHubApp")
        web.webViewClient = Client()
        // First start after an update: drop pages cached by the old version.
        val prefs = getSharedPreferences("app", MODE_PRIVATE)
        val code = Updater.installedVersionCode(this)
        if (prefs.getInt("last_version", 0) != code) {
            web.clearCache(true)
            prefs.edit().putInt("last_version", code).apply()
        }

        // Open at once - the page and its last snapshot come from the phone (ShellCache, localStorage).
        // Requests start on the route that worked last time; the LAN / public probe runs meanwhile and
        // only changes where the relay sends them, the page itself stays.
        Net.publicOverride = prefs.getString("public_url", null)
        prefs.getString("last_base", null)?.let { Net.assume(it) }
        loadHome()
        scope.launch {
            rememberRoute(Net.baseUrl(force = true))
            checkUpdate(silent = true)
        }
        getSystemService(ConnectivityManager::class.java).registerDefaultNetworkCallback(networkCallback)
    }

    // A network came back (or changed, e.g. Wi-Fi <-> 5G): re-probe the route and have the page reconnect
    // now instead of at its next retry.
    private val networkCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) {
            runOnUiThread {
                scope.launch {
                    rememberRoute(Net.baseUrl(force = true))
                    web.evaluateJavascript("window.connect && connect()", null)
                }
            }
        }
    }

    private fun rememberRoute(base: String) =
        getSharedPreferences("app", MODE_PRIVATE).edit().putString("last_base", base).apply()

    private var resumedOnce = false

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == SetupHotspot.REQ_LOCATION) hotspot.onPermissionResult(grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED)
    }

    override fun onResume() {
        super.onResume()
        if (!resumedOnce) { resumedOnce = true; return }   // first resume is part of onCreate's start-up
        // Back from the "allow installs" settings page with the apk already downloaded: install now.
        pendingInstall?.let { file ->
            if (packageManager.canRequestPackageInstalls()) {
                pendingInstall = null
                Updater.install(this, file)
            }
        }
        // Left home or came back: re-probe LAN vs public (the relay just follows). Reload only when a
        // deploy changed the page (ShellCache noticed while refreshing its copy in the background).
        scope.launch {
            rememberRoute(Net.baseUrl(force = true))
            if (shell.pageChanged) {
                shell.pageChanged = false
                loadHome()
            }
        }
    }

    override fun onDestroy() {
        getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(networkCallback)
        scope.cancel()
        super.onDestroy()
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        // The page decides first (window.hhBack: close a sheet, leave edit mode, leave a sub-page). When it
        // has nothing to undo, go to the background instead of finishing, so reopening shows the page as
        // it was. WebView history is not used: it held earlier loads of the same page and led to a blank one.
        web.evaluateJavascript("(window.hhBack ? window.hhBack() : false)") { handled ->
            if (handled != "true") moveTaskToBack(true)
        }
    }

    // ---- edge to edge -----------------------------------------------------------------

    /** Status / insets in CSS px, handed to the page (it pads its header by them). */
    @Volatile private var insetTopCss = 0f
    @Volatile private var insetBottomCss = 0f

    /**
     * The page's gradient runs up under a transparent status bar with white icons, like Mijia.
     * Android doesn't give WebView pages env(safe-area-inset-*), so the bar heights are passed in
     * through the bridge (insetTop / insetBottom) and pushed again whenever they change.
     */
    private fun edgeToEdge() {
        WindowCompat.setDecorFitsSystemWindows(window, false)
        @Suppress("DEPRECATION")
        window.statusBarColor = Color.TRANSPARENT
        @Suppress("DEPRECATION")
        window.navigationBarColor = Color.TRANSPARENT
        val night = (resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES
        WindowCompat.getInsetsController(window, web).apply {
            isAppearanceLightStatusBars = false          // white icons over the gradient's dark top
            isAppearanceLightNavigationBars = !night     // dark gesture bar over the light bottom by day
        }
        web.setBackgroundColor(Color.parseColor(if (night) "#1c1f33" else "#7f86b5"))
        ViewCompat.setOnApplyWindowInsetsListener(web) { _, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            val density = resources.displayMetrics.density
            insetTopCss = bars.top / density
            insetBottomCss = bars.bottom / density
            pushInsets()
            insets
        }
    }

    private fun pushInsets() {
        web.evaluateJavascript(
            "document.documentElement.style.setProperty('--sat','${insetTopCss}px');" +
                "document.documentElement.style.setProperty('--sab','${insetBottomCss}px');", null
        )
    }

    /** Set by loadHome: once that load finishes, forget the earlier loads (see onBackPressed). */
    private var clearHistoryWhenLoaded = false

    private fun loadHome() {
        clearHistoryWhenLoaded = true
        web.loadUrl("$ORIGIN/")
    }

    // ---- updates ---------------------------------------------------------------------

    private suspend fun checkUpdate(silent: Boolean) {
        val info = try {
            Updater.check(this)
        } catch (e: Exception) {
            if (!silent) toast("检查更新失败：${e.message}")
            return
        }
        if (info == null) {
            if (!silent) toast("已经是最新版 ${Updater.installedVersionName(this)}")
            return
        }
        val size = if (info.sizeBytes > 0) "（%.1f MB）".format(info.sizeBytes / 1_048_576.0) else ""
        AlertDialog.Builder(this)
            .setTitle("发现新版本 ${info.versionName}$size")
            .setMessage(info.notes.ifBlank { "更新到新版本" })
            .setPositiveButton("更新") { _, _ -> startUpdate(info) }
            .setNegativeButton("以后", null)
            .show()
    }

    private fun startUpdate(info: UpdateInfo) {
        val dialog = AlertDialog.Builder(this)
            .setTitle("正在下载 ${info.versionName}")
            .setMessage("0%")
            .setCancelable(false)
            .setNegativeButton("取消") { _, _ -> downloadJob?.cancel() }
            .show()
        downloadJob = scope.launch {
            try {
                val file = Updater.download(this@MainActivity, info) { p ->
                    runOnUiThread { dialog.setMessage("${(p * 100).toInt()}%") }
                }
                dialog.dismiss()
                if (!Updater.install(this@MainActivity, file)) {
                    pendingInstall = file
                    toast("请允许 HomeHub 安装应用，然后返回")
                }
            } catch (e: Exception) {
                dialog.dismiss()
                if (e !is kotlinx.coroutines.CancellationException) toast("下载失败：${e.message}")
            }
        }
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_LONG).show()

    // ---- WebView plumbing ------------------------------------------------------------

    private inner class Client : WebViewClient() {
        // The page lives on ORIGIN, which is not a real server: every request to it goes through
        // ShellCache - page files from the phone's own copy, API calls and the live stream relayed to the
        // hub over the pinned client, on whichever route (LAN / public) is current.
        override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? {
            if (request.url.host != ORIGIN_HOST) return null
            return shell.respond(request, Net.current())
        }

        override fun onPageFinished(view: WebView, url: String?) {
            pushInsets()
            if (clearHistoryWhenLoaded) {
                clearHistoryWhenLoaded = false
                view.clearHistory()
            }
        }

        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            val url = request.url.toString()
            if (request.url.host == ORIGIN_HOST) return false
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
            return true
        }
    }

    /** window.HomeHubApp in the page. Called on a WebView thread, so UI work hops to the main thread. */
    private inner class Bridge {
        @JavascriptInterface fun versionName(): String = Updater.installedVersionName(this@MainActivity)
        @JavascriptInterface fun route(): String = Net.routeName()
        /** "打开米家去添加": launch another app by package (declared under <queries> in the manifest). */
        @JavascriptInterface fun openApp(pkg: String) = runOnUiThread {
            packageManager.getLaunchIntentForPackage(pkg)?.let { startActivity(it) } ?: toast("手机上没有装这个应用")
        }
        /** Adding a Xiaomi Wi-Fi device: op = scan | join | udp | leave; the answer comes back via window.hhSetup(id, json). */
        @JavascriptInterface fun setup(op: String, id: String, args: String) {
            val a = JSONObject(args.ifBlank { "{}" })
            when (op) {
                "scan" -> runOnUiThread { hotspot.scan(id) }
                "join" -> runOnUiThread { hotspot.join(id, a.getString("ssid")) }
                "udp" -> hotspot.udp(id, a.getString("host"), a.optInt("port", 54321), a.getString("hex"), a.optInt("timeout", 4000))
                "leave" -> runOnUiThread { hotspot.leave(); web.evaluateJavascript("window.hhSetup && hhSetup(${JSONObject.quote(id)}, {\"ok\":true})", null) }
            }
        }
        /** The public address in use ("https://name:port"), and changing it: "" when saved, else the reason it was not. */
        @JavascriptInterface fun publicUrl(): String = Net.publicUrl()
        @JavascriptInterface fun setPublicUrl(input: String): String {
            val reset = input.isBlank()
            val url = if (reset) null else Net.normalizePublic(input) ?: return "这不是一个有效的地址，写成 域名 或 域名:端口"
            getSharedPreferences("app", MODE_PRIVATE).edit().apply { if (url == null) remove("public_url") else putString("public_url", url) }.apply()
            Net.publicOverride = url
            runOnUiThread { scope.launch { rememberRoute(Net.baseUrl(force = true)); web.evaluateJavascript("window.connect && connect()", null) } }
            return ""
        }
        @JavascriptInterface fun hasSetup(): Boolean = true
        @JavascriptInterface fun insetTop(): Float = insetTopCss
        @JavascriptInterface fun insetBottom(): Float = insetBottomCss
        @JavascriptInterface fun checkUpdate() = runOnUiThread { scope.launch { checkUpdate(silent = false) } }
        @JavascriptInterface fun retry() = runOnUiThread { scope.launch { rememberRoute(Net.baseUrl(force = true)); loadHome() } }
    }

    companion object {
        /**
         * The page's origin. Not a real server (see shouldInterceptRequest), so the page, its storage and its
         * cached snapshot are one and the same at home, outside and offline.
         */
        const val ORIGIN_HOST = "homehub.local"
        const val ORIGIN = "https://$ORIGIN_HOST"
    }
}
