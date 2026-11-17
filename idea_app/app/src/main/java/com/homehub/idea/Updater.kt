package com.homehub.idea

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import androidx.core.content.FileProvider
import androidx.core.content.pm.PackageInfoCompat
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import okhttp3.Request
import okhttp3.Response
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import kotlin.coroutines.coroutineContext

data class UpdateInfo(val versionCode: Int, val versionName: String, val apk: String, val notes: String, val sizeBytes: Long)

/** Self-update: tools/journey/publish_idea.ps1 puts the apk and version.json on the hub (/api/journey/app/), behind the token. */
object Updater {
    fun versionCode(context: Context): Int =
        PackageInfoCompat.getLongVersionCode(context.packageManager.getPackageInfo(context.packageName, 0)).toInt()

    fun versionName(context: Context): String = context.packageManager.getPackageInfo(context.packageName, 0).versionName ?: ""

    private suspend fun get(path: String): Response {
        val req = Request.Builder().url(Net.baseUrl() + path).header("Authorization", "Bearer ${BuildConfig.TOKEN}").build()
        val resp = Net.client.newCall(req).execute()
        if (!resp.isSuccessful) { resp.close(); throw IllegalStateException(if (resp.code == 404) "服务器上还没有发布过安装包" else "服务器返回 ${resp.code}") }
        return resp
    }

    suspend fun check(context: Context): UpdateInfo? = withContext(Dispatchers.IO) {
        val o = get("/api/journey/app/version.json").use { JSONObject(it.body!!.string()) }
        val code = o.getInt("versionCode")
        if (code <= versionCode(context)) return@withContext null
        val notes = when (val raw = o.opt("notes")) {
            is JSONArray -> (0 until raw.length()).joinToString("\n") { "· " + raw.getString(it) }
            is String -> raw
            else -> ""
        }
        UpdateInfo(code, o.optString("versionName", code.toString()), o.getString("apk"), notes, o.optLong("size"))
    }

    suspend fun download(context: Context, info: UpdateInfo, onProgress: (Float) -> Unit): File = withContext(Dispatchers.IO) {
        val dir = File(context.cacheDir, "updates").apply { mkdirs() }
        val file = File(dir, "update-${info.versionCode}.apk")
        if (file.exists() && file.length() > 0) return@withContext file
        dir.listFiles()?.forEach { it.delete() }
        val part = File(dir, "update.part")
        try {
            get("/api/journey/app/${info.apk}").use { resp ->
                val body = resp.body!!
                val total = body.contentLength()
                body.byteStream().use { input ->
                    part.outputStream().use { out ->
                        val buf = ByteArray(64 * 1024)
                        var done = 0L
                        while (true) {
                            coroutineContext.ensureActive()
                            val n = input.read(buf)
                            if (n < 0) break
                            out.write(buf, 0, n); done += n
                            if (total > 0) onProgress((done.toFloat() / total).coerceIn(0f, 1f))
                        }
                    }
                }
                if (total > 0 && part.length() != total) error("下载不完整，再试一次")
            }
            if (!part.renameTo(file)) error("保存安装包失败")
        } catch (e: Exception) { part.delete(); throw e }
        file
    }

    /** Opens the system installer; false when the "install unknown apps" permission page had to be shown first. */
    fun install(context: Context, file: File): Boolean {
        if (!context.packageManager.canRequestPackageInstalls()) {
            context.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${context.packageName}"))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            return false
        }
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
        context.startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/vnd.android.package-archive")
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK))
        return true
    }
}
