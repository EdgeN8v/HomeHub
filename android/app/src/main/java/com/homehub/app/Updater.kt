package com.homehub.app

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

/**
 * Self-update, same scheme as Music / LearnSync: tools/publish_app.ps1 puts the apk and version.json
 * in the hub's data/updates/, served at /api/updates/ behind the token and the pinned TLS - so it
 * works from outside home too.
 */
object Updater {
    fun installedVersionCode(context: Context): Int =
        PackageInfoCompat.getLongVersionCode(context.packageManager.getPackageInfo(context.packageName, 0)).toInt()

    fun installedVersionName(context: Context): String =
        context.packageManager.getPackageInfo(context.packageName, 0).versionName ?: ""

    private suspend fun get(path: String): Response {
        val req = Request.Builder().url(Net.baseUrl() + path).header("Authorization", "Bearer ${BuildConfig.TOKEN}").build()
        val resp = Net.client.newCall(req).execute()
        if (resp.code == 404) {
            resp.close()
            throw IllegalStateException("服务器上还没有发布过安装包")
        }
        if (!resp.isSuccessful) {
            resp.close()
            throw IllegalStateException("服务器返回 ${resp.code}")
        }
        return resp
    }

    /** The newer release, or null when already up to date. */
    suspend fun check(context: Context): UpdateInfo? = withContext(Dispatchers.IO) {
        val obj = get("/api/updates/version.json").use { JSONObject(it.body!!.string()) }
        val code = obj.getInt("versionCode")
        if (code <= installedVersionCode(context)) return@withContext null
        val notes = when (val raw = obj.opt("notes")) {
            is JSONArray -> (0 until raw.length()).joinToString("\n") { "· " + raw.getString(it) }
            is String -> raw
            else -> ""
        }
        UpdateInfo(code, obj.optString("versionName", code.toString()), obj.getString("apk"), notes, obj.optLong("size"))
    }

    /** Downloads into cache/updates (kept and reused if the install detours through the permission page). */
    suspend fun download(context: Context, info: UpdateInfo, onProgress: (Float) -> Unit): File =
        withContext(Dispatchers.IO) {
            val dir = File(context.cacheDir, "updates").apply { mkdirs() }
            val file = File(dir, "update-${info.versionCode}.apk")
            if (file.exists() && file.length() > 0) {
                onProgress(1f)
                return@withContext file
            }
            dir.listFiles()?.forEach { it.delete() }
            val part = File(dir, "update-${info.versionCode}.apk.part")
            try {
                get("/api/updates/${info.apk}").use { resp ->
                    val body = resp.body!!
                    val total = body.contentLength()
                    body.byteStream().use { input ->
                        part.outputStream().use { out ->
                            val buffer = ByteArray(64 * 1024)
                            var done = 0L
                            while (true) {
                                coroutineContext.ensureActive()
                                val n = input.read(buffer)
                                if (n < 0) break
                                out.write(buffer, 0, n)
                                done += n
                                if (total > 0) onProgress((done.toFloat() / total).coerceIn(0f, 1f))
                            }
                        }
                    }
                    if (total > 0 && part.length() != total) error("下载不完整，再试一次")
                }
                if (!part.renameTo(file)) error("保存安装包失败")
            } catch (e: Exception) {
                part.delete()
                throw e
            }
            file
        }

    /** Opens the system installer; false when the "install unknown apps" permission page had to be shown first. */
    fun install(context: Context, file: File): Boolean {
        if (!context.packageManager.canRequestPackageInstalls()) {
            context.startActivity(
                Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${context.packageName}"))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            )
            return false
        }
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
        context.startActivity(
            Intent(Intent.ACTION_VIEW)
                .setDataAndType(uri, "application/vnd.android.package-archive")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
        )
        return true
    }
}
