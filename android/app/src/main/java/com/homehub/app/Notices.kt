package com.homehub.app

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.job.JobInfo
import android.app.job.JobParameters
import android.app.job.JobScheduler
import android.app.job.JobService
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.time.Instant
import java.util.concurrent.TimeUnit

/**
 * Phone notifications from the hub (防冻提醒 and later washer-finished and the like), without any push service.
 *
 * The hub numbers its notices (hub/app/notify.py). This phone remembers the last number it has shown and asks for newer ones:
 * - in the background, every ~15 min through JobScheduler (the shortest period Android allows; survives reboots), and
 * - while the app is open, with a long poll that the hub answers the moment a notice is raised.
 * Each notice becomes one Android notification (its number is the notification id, so asking twice shows it once).
 */
object Notices {
    private const val CHANNEL = "reminders"
    private const val JOB_ID = 4201
    private const val MAX_AGE_S = 24 * 3600L        // a phone that was off for days does not get yesterday's news

    fun setup(context: Context) {
        val nm = context.getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "设备提醒", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "热水器防冻等设备提醒"
            })
        }
        val js = context.getSystemService(JobScheduler::class.java)
        if (js.getPendingJob(JOB_ID) == null) {
            js.schedule(JobInfo.Builder(JOB_ID, ComponentName(context, NoticeJob::class.java))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setPeriodic(TimeUnit.MINUTES.toMillis(15))
                .setPersisted(true)
                .build())
        }
    }

    fun allowed(context: Context): Boolean =
        android.os.Build.VERSION.SDK_INT < 33 ||
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    private val lock = Any()

    /**
     * Ask the hub for notices newer than the last one shown, waiting up to [waitS] seconds for one to come, and show them.
     * Returns false when the hub could not be reached. Blocking: call off the main thread.
     */
    suspend fun poll(context: Context, waitS: Int): Boolean {
        val prefs = context.getSharedPreferences("app", Context.MODE_PRIVATE)
        val after = prefs.getLong("notice_after", -1)
        val base = Net.baseUrl()
        val text = try {
            Net.client.newBuilder().readTimeout(waitS + 15L, TimeUnit.SECONDS).build().newCall(
                okhttp3.Request.Builder().url("$base/api/notices?after=$after&wait=$waitS")
                    .header("Authorization", "Bearer ${BuildConfig.TOKEN}").build()
            ).execute().use { if (it.isSuccessful) it.body?.string() else null }
        } catch (e: Exception) { null } ?: run { Net.failover(); return false }
        val obj = try { JSONObject(text) } catch (e: Exception) { return false }
        val list = obj.optJSONArray("notices")
        val now = Instant.now().epochSecond
        for (i in 0 until (list?.length() ?: 0)) {
            val n = list!!.getJSONObject(i)
            val age = try { now - Instant.parse(n.getString("ts").replace("+00:00", "Z")).epochSecond } catch (e: Exception) { 0 }
            if (age < MAX_AGE_S) show(context, n.getInt("id"), n.getString("title"), n.optString("body"))
        }
        synchronized(lock) {
            val latest = obj.optLong("latest", after)
            if (latest > prefs.getLong("notice_after", -1)) prefs.edit().putLong("notice_after", latest).apply()
        }
        return true
    }

    private fun show(context: Context, id: Int, title: String, body: String) {
        if (!allowed(context)) return
        val open = PendingIntent.getActivity(context, 0,
            Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = NotificationCompat.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_notice)
            .setColor(Color.parseColor("#2F80ED"))
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(NotificationCompat.CATEGORY_REMINDER)
            .setAutoCancel(true)
            .setContentIntent(open)
            .build()
        try { NotificationManagerCompat.from(context).notify(id, n) } catch (e: SecurityException) { /* permission revoked meanwhile */ }
    }

    /** Net needs the saved address table and the chosen public address also when only the background job runs. */
    fun initNet(context: Context) {
        if (Net.base != null) return
        Net.init(context)
        val prefs = context.getSharedPreferences("app", Context.MODE_PRIVATE)
        Net.publicOverride = prefs.getString("public_url", null)
        prefs.getString("last_base", null)?.let { Net.assume(it) }
    }
}

/** The background check (every ~15 min, any network). */
class NoticeJob : JobService() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun onStartJob(params: JobParameters): Boolean {
        scope.launch {
            Notices.initNet(applicationContext)
            // a short wait: a notice raised a moment later still arrives now rather than in 15 min
            val ok = withContext(Dispatchers.IO) { Notices.poll(applicationContext, 20) }
            jobFinished(params, !ok)
        }
        return true
    }

    override fun onStopJob(params: JobParameters): Boolean = true
}
