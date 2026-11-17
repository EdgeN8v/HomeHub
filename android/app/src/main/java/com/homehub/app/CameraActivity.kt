package com.homehub.app

import android.Manifest
import android.app.Activity
import android.app.Dialog
import android.content.ContentValues
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Bundle
import android.provider.MediaStore
import android.view.Gravity
import android.view.MotionEvent
import android.view.TextureView
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.view.animation.AccelerateInterpolator
import android.view.animation.DecelerateInterpolator
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.SeekBar
import android.widget.TextView
import android.widget.Toast
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.VideoSize
import androidx.media3.datasource.okhttp.OkHttpDataSource
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okio.BufferedSink
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.TimeUnit
import kotlin.math.abs
import kotlin.math.min

/**
 * The camera page, laid out like Mijia's: live picture on top (hardware-decoded H.265 from the hub, started at once from the hub's
 * own copy of the stream), tools (talk, snapshot, record, direction, settings), the direction pad and the list of recorded movements.
 * Landscape full screen puts the pad and the buttons over the picture.
 */
class CameraActivity : Activity() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var player: ExoPlayer? = null
    private var src = "c700"
    private var retry = 0
    private var live = true
    private var full = false
    private var muted = true
    private var powered = false
    private var cameraOffline = true
    private var checkingPower = true
    private var powerBusy = false
    private var selected = ""
    private var base = ""

    private lateinit var box: FrameLayout
    private lateinit var surface: TextureView
    private lateinit var still: ImageView
    private lateinit var layer: FrameLayout
    private lateinit var status: TextView
    private lateinit var loading: LoadingPercent
    private lateinit var powerBtn: IconView
    private lateinit var title: TextView
    private lateinit var muteBtn: IconView
    private lateinit var liveChip: TextView
    private lateinit var talkHint: View
    private lateinit var recBtn: IconView
    private lateinit var recLabel: TextView
    private lateinit var scroll: View
    private lateinit var eventsCol: LinearLayout
    private lateinit var wheelPanel: LinearLayout
    private lateinit var header: View
    private lateinit var fullPad: View
    private lateinit var fullTools: View
    private lateinit var bar: View
    private lateinit var sleepPage: FrameLayout
    private lateinit var talkTool: IconView
    private lateinit var talkLabel: TextView
    private lateinit var fullMic: IconView
    private var barJob: Job? = null
    private lateinit var qualityChip: TextView
    private lateinit var micPill: LinearLayout
    private lateinit var pillMic: IconView
    private var quality = "hd"

    private val density get() = resources.displayMetrics.density
    private fun dp(v: Int) = (v * density).toInt()
    private val ink = Color.parseColor("#222222")
    private val teal = Color.parseColor("#2BB8B0")
    private val red = Color.parseColor("#FF6B6B")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        src = intent.getStringExtra("src") ?: "c700"
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        WindowCompat.setDecorFitsSystemWindows(window, true)
        buildUi()
        startPlayer()
        if (intent.getStringExtra("clip").isNullOrEmpty()) showSleep(false)
        scope.launch {
            base = try { Net.baseUrl() } catch (e: Exception) { "" }
            val clip = intent.getStringExtra("clip")
            if (clip.isNullOrEmpty()) {
                refreshCamera()
                launch {
                    while (true) {
                        delay(10000)
                        if (live && !powerBusy) refreshCamera()
                    }
                }
            } else playClip(clip, clip.substringBeforeLast('.'))
            launch { getJson("/api/cam/prefs")?.optString("name")?.takeIf { it.isNotEmpty() }?.let { title.text = it } }
            loadEvents()
        }
    }

    // ---- layout --------------------------------------------------------------------------------------------------------------
    private fun round(color: Int, r: Int = 14) = GradientDrawable().apply { setColor(color); cornerRadius = dp(r).toFloat() }

    private fun tool(kind: String, label: String, onClick: (() -> Unit)?): Pair<View, IconView> {
        val icon = IconView(this, kind, Hh.ink).apply { layoutParams = LinearLayout.LayoutParams(dp(30), dp(30)) }
        val v = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL; gravity = Gravity.CENTER
            addView(icon)
            addView(TextView(this@CameraActivity).apply { text = label; textSize = 12f; gravity = Gravity.CENTER; setTextColor(Hh.sub); setPadding(0, dp(6), 0, 0) })
            if (onClick != null) setOnClickListener { onClick() }
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f).apply { topMargin = dp(12); bottomMargin = dp(12) }
        }
        return v to icon
    }

    private fun buildUi() {
        val root = FrameLayout(this).apply { background = Hh.sky }
        window.statusBarColor = Color.parseColor("#7F86B5")
        window.navigationBarColor = Color.parseColor("#F1E4DD")
        WindowInsetsControllerCompat(window, window.decorView).apply { isAppearanceLightStatusBars = false; isAppearanceLightNavigationBars = true }
        val content = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(content, FrameLayout.LayoutParams(-1, -1))

        header = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL; setPadding(dp(6), dp(4), dp(12), dp(4))
            addView(IconView(this@CameraActivity, "back", Color.WHITE).apply { setPadding(dp(11), dp(11), dp(11), dp(11)); setOnClickListener { finish() } }, LinearLayout.LayoutParams(dp(46), dp(46)))
            title = TextView(this@CameraActivity).apply { text = "小米智能摄像机 C700"; textSize = 18f; setTypeface(null, Typeface.BOLD); setTextColor(Color.WHITE); setPadding(dp(4), 0, 0, 0) }
            addView(title)
        }
        content.addView(header, LinearLayout.LayoutParams(-1, -2))

        // the picture and its "loading" text sit in one layer so switching the camera off can fold it up and on can unfold it, like Mijia
        box = FrameLayout(this).apply { setBackgroundColor(Color.parseColor("#17181C")) }
        layer = FrameLayout(this).apply { setBackgroundColor(Color.BLACK) }
        surface = TextureView(this)
        layer.addView(surface, FrameLayout.LayoutParams(-1, -1, Gravity.CENTER))
        still = ImageView(this).apply { scaleType = ImageView.ScaleType.CENTER_CROP; scaleX = 1.035f; scaleY = 1.035f; visibility = View.GONE }
        // the card's live frame if it was playing, else the hub's latest picture (at most 2 minutes old) while the player connects
        (CamHandoff.take() ?: java.io.File(filesDir, "campics/latest.jpg").takeIf { intent.getStringExtra("clip") == null && it.exists() && System.currentTimeMillis() - it.lastModified() < 120_000 }
            ?.let { BitmapFactory.decodeFile(it.path) })?.let { still.setImageBitmap(soft(it)); still.visibility = View.VISIBLE }
        if (android.os.Build.VERSION.SDK_INT >= 31) still.setRenderEffect(android.graphics.RenderEffect.createBlurEffect(dp(14).toFloat(), dp(14).toFloat(), android.graphics.Shader.TileMode.CLAMP))
        layer.addView(still, FrameLayout.LayoutParams(-1, -1))
        status = TextView(this).apply { setTextColor(Color.WHITE); textSize = 15f; setShadowLayer(4f, 0f, 0f, Color.BLACK) }
        layer.addView(status, FrameLayout.LayoutParams(-2, -2, Gravity.CENTER))
        loading = LoadingPercent(scope, status)
        box.addView(layer, FrameLayout.LayoutParams(-1, -1))
        layer.addOnLayoutChangeListener { v, _, _, _, _, _, _, _, _ -> v.pivotY = 0f }
        sleepPage = buildSleepPage()
        box.addView(sleepPage, FrameLayout.LayoutParams(-1, -1))
        liveChip = TextView(this).apply {
            text = "回到直播"; textSize = 12f; setTextColor(Color.WHITE); setPadding(dp(12), dp(5), dp(12), dp(5)); background = round(0x99000000.toInt(), 14); visibility = View.GONE
            setOnClickListener { playLive() }
        }
        box.addView(liveChip, FrameLayout.LayoutParams(-2, -2, Gravity.TOP or Gravity.START).apply { topMargin = dp(10); leftMargin = dp(10) })
        // while talking: a clear pill on the picture, its microphone fills with colour from the bottom as the voice gets louder
        micPill = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL; setPadding(dp(14), dp(8), dp(18), dp(8)); visibility = View.GONE
            background = round(0xD9101216.toInt(), 28)
            pillMic = IconView(this@CameraActivity, "mic", Color.parseColor("#9AA0AE"), 1.1f).apply { accent = Color.parseColor("#2EE6A8") }
            addView(pillMic, LinearLayout.LayoutParams(dp(40), dp(40)))
            addView(TextView(this@CameraActivity).apply { text = "正在说话，再点一下结束"; textSize = 13f; setTextColor(Color.WHITE); setPadding(dp(10), 0, 0, 0) })
        }
        talkHint = micPill
        box.addView(micPill, FrameLayout.LayoutParams(-2, -2, Gravity.CENTER))
        // power, sound, enlarge: soft dark glass buttons over the picture; they hide after two seconds and a tap on the picture brings them back
        bar = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL; setPadding(dp(8), dp(26), dp(8), dp(6))
            background = GradientDrawable(GradientDrawable.Orientation.BOTTOM_TOP, intArrayOf(0x73000000, 0x00000000))
            fun b(kind: String, f: () -> Unit) = IconView(this@CameraActivity, kind, Color.WHITE, 1.05f).apply {
                setPadding(dp(10), dp(10), dp(10), dp(10)); setOnClickListener { f(); showBar() }
                layoutParams = LinearLayout.LayoutParams(dp(38), dp(38))
            }
            powerBtn = b("power") { togglePower() }
            addView(powerBtn)
            muteBtn = b("mute") { toggleMute() }
            addView(muteBtn)
            recLabel = TextView(this@CameraActivity).apply { textSize = 12f; setTextColor(red); setPadding(dp(6), 0, 0, 0) }
            addView(recLabel)
            addView(View(this@CameraActivity), LinearLayout.LayoutParams(0, 1, 1f))
            qualityChip = TextView(this@CameraActivity).apply {
                text = "1080P"; textSize = 11f; setTypeface(null, Typeface.BOLD); setTextColor(Color.WHITE); gravity = Gravity.CENTER
                setPadding(dp(7), dp(2), dp(7), dp(2))
                background = GradientDrawable().apply { cornerRadius = dp(5).toFloat(); setStroke(dp(1), 0xD9FFFFFF.toInt()) }
                setOnClickListener { pickQuality(this); showBar() }
            }
            addView(qualityChip, LinearLayout.LayoutParams(-2, -2).apply { rightMargin = dp(6) })
            addView(b("expand") { toggleFull() })
        }
        box.addView(bar, FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM))
        box.setOnClickListener { if (bar.alpha > 0.5f && bar.visibility == View.VISIBLE) hideBar() else showBar() }
        showBar()
        content.addView(box, LinearLayout.LayoutParams(-1, dp(210)))
        box.addOnLayoutChangeListener { _, l, _, r, _, _, _, _, _ ->
            if (!full) {
                val w = r - l; val lp = box.layoutParams; val h = w * 9 / 16
                if (w > 0 && lp.height != h) { lp.height = h; box.layoutParams = lp }
            }
        }

        // below: tools, pad, events (scrolls)
        val sv = ScrollView(this).apply { isFillViewport = true }
        scroll = sv
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(14), dp(12), dp(14), dp(24)) }
        sv.addView(col)
        content.addView(sv, LinearLayout.LayoutParams(-1, 0, 1f))

        val tools = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; background = Hh.card(this@CameraActivity) }
        val (talkView, talkIcon) = tool("mic", "点击说话") { toggleTalk() }
        talkTool = talkIcon
        talkLabel = ((talkView as LinearLayout).getChildAt(1)) as TextView
        tools.addView(talkView)
        tools.addView(tool("camera", "截图") { snapshot() }.first)
        val (recView, ri) = tool("record", "录像") { toggleRecord() }
        recBtn = ri
        tools.addView(recView)
        tools.addView(tool("move", "方向") { wheelPanel.visibility = if (wheelPanel.visibility == View.VISIBLE) View.GONE else View.VISIBLE }.first)
        tools.addView(tool("settings", "设置") { showSettings() }.first)
        col.addView(tools, LinearLayout.LayoutParams(-1, -2))

        wheelPanel = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; gravity = Gravity.CENTER; background = Hh.card(this@CameraActivity); visibility = View.VISIBLE }
        wheelPanel.addView(Wheel(this, false) { dir -> moveHead(dir) }, LinearLayout.LayoutParams(dp(230), dp(230)).apply { topMargin = dp(14); bottomMargin = dp(14) })
        col.addView(wheelPanel, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(12) })

        col.addView(TextView(this).apply { text = "看家助手"; textSize = 17f; setTypeface(null, Typeface.BOLD); setTextColor(Hh.ink); setPadding(dp(4), dp(18), 0, dp(8)) })
        eventsCol = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; background = Hh.card(this@CameraActivity); setPadding(dp(10), dp(6), dp(10), dp(6)) }
        col.addView(eventsCol, LinearLayout.LayoutParams(-1, -2))

        // landscape overlays: the pad bottom-left, the buttons on the right
        fullPad = Wheel(this, true) { dir -> moveHead(dir) }.apply { visibility = View.GONE }
        root.addView(fullPad, FrameLayout.LayoutParams(dp(150), dp(150), Gravity.BOTTOM or Gravity.START).apply { leftMargin = dp(30); bottomMargin = dp(40) })
        fullTools = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL; gravity = Gravity.CENTER; visibility = View.GONE
            fun b(kind: String, f: () -> Unit) = IconView(this@CameraActivity, kind).apply { setPadding(dp(14), dp(14), dp(14), dp(14)); background = round(0x66000000, 28); setOnClickListener { f() } }
            addView(b("camera") { snapshot() }, LinearLayout.LayoutParams(dp(54), dp(54)).apply { bottomMargin = dp(14) })
            addView(b("record") { toggleRecord() }, LinearLayout.LayoutParams(dp(54), dp(54)).apply { bottomMargin = dp(14) })
            fullMic = IconView(this@CameraActivity, "mic").apply { setPadding(dp(14), dp(14), dp(14), dp(14)); background = round(0x66000000, 28); setOnClickListener { toggleTalk() } }
            addView(fullMic, LinearLayout.LayoutParams(dp(54), dp(54)))
        }
        root.addView(fullTools, FrameLayout.LayoutParams(-2, -2, Gravity.END or Gravity.CENTER_VERTICAL).apply { rightMargin = dp(24) })
        setContentView(root)
    }

    // ---- player --------------------------------------------------------------------------------------------------------------
    private var vidW = 0
    private var vidH = 0

    private fun fit() {
        if (vidW == 0 || vidH == 0) return
        val ratio = vidW.toFloat() / vidH
        var vw = box.width; var vh = (vw / ratio).toInt()
        if (box.height > 0 && vh > box.height) { vh = box.height; vw = (vh * ratio).toInt() }
        val params = FrameLayout.LayoutParams(vw, vh, Gravity.CENTER)
        surface.layoutParams = params
        surface.scaleX = 1.035f; surface.scaleY = 1.035f      // the stream has a thin black rim of its own: crop it off
    }

    private fun startPlayer() {
        val http = OkHttpDataSource.Factory(Net.client).setDefaultRequestProperties(mapOf("Authorization" to "Bearer ${BuildConfig.TOKEN}"))
        val load = DefaultLoadControl.Builder().setBufferDurationsMs(500, 8000, 250, 500).build()
        val p = ExoPlayer.Builder(this).setLoadControl(load).setMediaSourceFactory(DefaultMediaSourceFactory(http)).build()
        player = p
        p.volume = 0f
        p.addListener(object : Player.Listener {
            override fun onVideoSizeChanged(size: VideoSize) { if (size.width > 0) { vidW = (size.width * size.pixelWidthHeightRatio).toInt(); vidH = size.height }; box.post { fit() } }

            // once a picture is up, a short wait for data just holds the last frame (like Mijia); "加载中" only after 2.5 s of nothing
            override fun onRenderedFirstFrame() {
                shown = true; stallJob?.cancel(); loading.done()
                if (still.visibility == View.VISIBLE) still.animate().alpha(0f).setDuration(200).withEndAction { still.visibility = View.GONE }.start()
            }

            override fun onPlaybackStateChanged(state: Int) {
                if (state == Player.STATE_READY) { stallJob?.cancel(); loading.done(); retry = 0 }
                if (state == Player.STATE_BUFFERING && powered) {
                    stallJob?.cancel()
                    if (shown || still.visibility == View.VISIBLE) stallJob = scope.launch { delay(2500); loading.start() } else loading.start()
                }
                if (state == Player.STATE_ENDED && !live) { loading.fail("播放完了"); liveChip.visibility = View.VISIBLE }
            }

            override fun onPlayerError(error: PlaybackException) {
                if (!live) loading.fail("这段录像放不了，点「回到直播」")
            }
        })
        p.playWhenReady = true

        // live: frames straight to the hardware decoder, shown as they come (LivePlayer) - no playback buffer, like Mijia
        livePlayer = LivePlayer(object : LivePlayer.Listener {
            override fun onFirstFrame() {
                shown = true; retry = 0; loading.done()
                if (still.visibility == View.VISIBLE) still.animate().alpha(0f).setDuration(200).withEndAction { still.visibility = View.GONE }.start()
            }
            override fun onVideoSize(w: Int, h: Int) { vidW = w; vidH = h; box.post { fit() } }
            override fun onStall(stalled: Boolean) { if (stalled && powered) loading.start() else loading.done() }
            override fun onError(msg: String) {
                if (!live || !powered || isFinishing) return
                if (!shown && still.visibility != View.VISIBLE) loading.fail("正在连接摄像头…")
                scope.launch { delay(1000L * minOf(++retry, 4)); if (live && powered) playLive() }
            }
        })
        surface.surfaceTextureListener = object : TextureView.SurfaceTextureListener {
            override fun onSurfaceTextureAvailable(st: android.graphics.SurfaceTexture, w: Int, h: Int) { liveSurface = android.view.Surface(st); pendingLive?.invoke() }
            override fun onSurfaceTextureSizeChanged(st: android.graphics.SurfaceTexture, w: Int, h: Int) {}
            override fun onSurfaceTextureDestroyed(st: android.graphics.SurfaceTexture): Boolean { livePlayer?.stop(); player?.clearVideoTextureView(surface); liveSurface = null; return true }
            override fun onSurfaceTextureUpdated(st: android.graphics.SurfaceTexture) {}
        }
    }

    private var livePlayer: LivePlayer? = null
    private var liveSurface: android.view.Surface? = null
    private var pendingLive: (() -> Unit)? = null

    /** Nothing plays any more (camera switched off, quality being changed). */
    private fun stopPicture() { player?.stop(); livePlayer?.stop() }
    /** The hub hands a new viewer the picture from the last keyframe (up to ~4 s old); playing it at normal speed would keep that delay for good.
     *  So the player runs fast while it has a backlog and drops to normal speed once it is almost live. */
    private fun catchUp(p: ExoPlayer, isLive: () -> Boolean): Job = scope.launch {
        while (true) {
            delay(120)
            val buf = p.totalBufferedDuration
            val cur = p.playbackParameters.speed
            val want = when {
                !isLive() || !p.isPlaying -> 1f
                buf > 3000 -> 2f
                buf > 1400 -> 1.3f
                buf > 700 && cur > 1f -> 1.1f
                else -> 1f
            }
            if (want != cur) p.setPlaybackSpeed(want)
        }
    }

    private var shown = false
    private var stallJob: Job? = null

    private fun playLive() {
        live = true; liveChip.visibility = View.GONE; selected = ""; markSelected()
        if (!shown && still.visibility != View.VISIBLE) surface.bitmap?.let {
            still.setImageBitmap(soft(it)); still.alpha = 1f; still.visibility = View.VISIBLE
        }
        if (!shown) loading.fail("正在连接直播 · 背景为上次画面")
        player?.stop(); player?.clearVideoTextureView(surface)          // the surface goes to the live decoder
        vidW = 0; vidH = 0
        surface.visibility = View.VISIBLE
        scope.launch {
            if (base.isEmpty()) base = try { Net.baseUrl() } catch (e: Exception) { loading.fail("连不上服务器"); return@launch }
            val go = { liveSurface?.let { sf -> if (live && powered) livePlayer?.start("$base/api/cam/live", sf) } }
            if (liveSurface != null) go() else pendingLive = { pendingLive = null; go() }
        }
    }

    private fun playClip(name: String, id: String) {
        live = false; liveChip.visibility = View.VISIBLE; selected = id; markSelected()
        if (!shown) loading.start()
        livePlayer?.stop()                                               // let go of the surface first
        surface.visibility = View.VISIBLE
        player?.apply { setVideoTextureView(surface); setMediaItem(MediaItem.fromUri("$base/api/cam/file/$name")); prepare() }
    }

    // ---- events --------------------------------------------------------------------------------------------------------------
    private suspend fun getJson(path: String): JSONObject? = withContext(Dispatchers.IO) {
        try {
            val req = Request.Builder().url("$base$path").header("Authorization", "Bearer ${BuildConfig.TOKEN}").build()
            Net.client.newCall(req).execute().use { if (it.isSuccessful) JSONObject(it.body!!.string()) else null }
        } catch (e: Exception) { null }
    }

    private suspend fun loadEvents() {
        val j = getJson("/api/cam/events")
        eventsCol.removeAllViews()
        val arr = j?.optJSONArray("events")
        if (arr == null || arr.length() == 0) {
            eventsCol.addView(TextView(this).apply {
                text = if (j == null) "读不到记录" else "还没有记录。有人或宠物在画面里走动时，这里会出现一条，点开看那一段。"
                textSize = 13f; setTextColor(Color.parseColor("#888888")); setPadding(dp(8), dp(16), dp(8), dp(16))
            })
            return
        }
        val fmt = SimpleDateFormat("MM-dd HH:mm", Locale.CHINA)
        for (i in 0 until min(arr.length(), 60)) {
            val e = arr.getJSONObject(i)
            val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL; setPadding(dp(4), dp(8), dp(4), dp(8)) }
            val img = ImageView(this).apply { scaleType = ImageView.ScaleType.CENTER_CROP; background = round(0x14000000, 10); clipToOutline = true }
            row.addView(img, LinearLayout.LayoutParams(dp(112), dp(63)))
            row.addView(LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL; setPadding(dp(14), 0, 0, 0)
                addView(TextView(this@CameraActivity).apply { text = if (e.optInt("dur") >= 3) "有人移动" else "有动静"; textSize = 15f; setTextColor(Hh.ink) })
                addView(TextView(this@CameraActivity).apply { text = fmt.format(Date(e.getLong("ts") * 1000)) + "  ·  持续约 ${e.optInt("dur")} 秒"; textSize = 12f; setTextColor(Hh.sub); setPadding(0, dp(3), 0, 0) })
            })
            val clip = e.optString("clip"); val id = e.optString("id")
            row.tag = id
            if (id == selected) row.background = round(Color.parseColor("#E6F7F6"), 12)
            row.setOnClickListener { if (clip.isNotEmpty()) { playClip(clip, id); (scroll as? ScrollView)?.smoothScrollTo(0, 0) } }
            eventsCol.addView(row)
            e.optString("thumb").takeIf { it.isNotEmpty() && it != "null" }?.let { t -> scope.launch { bitmap("/api/cam/file/$t")?.let { img.setImageBitmap(it) } } }
        }
    }

    private fun markSelected() {
        if (!::eventsCol.isInitialized) return
        for (i in 0 until eventsCol.childCount) {
            val v = eventsCol.getChildAt(i)
            v.background = if (v.tag == selected && selected.isNotEmpty()) round(Color.parseColor("#E6F7F6"), 12) else null
        }
    }

    private suspend fun bitmap(path: String): Bitmap? = withContext(Dispatchers.IO) {
        val name = Regex("""^/api/cam/file/([\w.-]+\.jpg)$""").find(path)?.groupValues?.get(1)
        val file = name?.let { java.io.File(java.io.File(filesDir, "campics").apply { mkdirs() }, it) }
        if (file != null && name != "latest.jpg" && file.length() > 0) BitmapFactory.decodeFile(file.path)?.let { return@withContext it }
        try {
            val req = Request.Builder().url("$base$path").header("Authorization", "Bearer ${BuildConfig.TOKEN}").build()
            Net.client.newCall(req).execute().use { r ->
                if (!r.isSuccessful) return@use null
                val bytes = r.body!!.bytes()
                if (file != null && bytes.isNotEmpty()) runCatching { java.io.File(file.path + ".tmp").apply { writeBytes(bytes); renameTo(file) } }
                BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
            }
        } catch (e: Exception) { null }
    }

    // ---- buttons -------------------------------------------------------------------------------------------------------------
    private fun toast(t: String) = Toast.makeText(this, t, Toast.LENGTH_SHORT).show()

    private fun toggleMute() { muted = !muted; applyVolume(); muteBtn.set(if (muted) "mute" else "volume") }
    private fun applyVolume() { player?.volume = if (muted || talking) 0f else 1f; livePlayer?.muted = muted || talking }

    // ---- power. Switching off: the picture stays up while the camera lowers its own head (that is the animation), then the "closed" page
    // fades in. Switching on: the page fades out and the picture comes back while the camera raises its head.
    private lateinit var sleepImg: ImageView
    private lateinit var sleepBtn: FrameLayout
    private lateinit var sleepIcon: IconView
    private lateinit var sleepSpin: android.widget.ProgressBar
    private lateinit var sleepText: TextView

    private fun buildSleepPage(): FrameLayout {
        val page = FrameLayout(this).apply {
            setBackgroundColor(Color.parseColor("#2A2A2E")); visibility = View.GONE; alpha = 0f
            setOnClickListener { if (!powered && sleepIcon.visibility == View.VISIBLE) togglePower() }
        }
        sleepImg = ImageView(this).apply {
            scaleType = ImageView.ScaleType.CENTER_CROP
            // blur(14px) grayscale(.6) brightness(.55), like the card
            val m = android.graphics.ColorMatrix().apply { setSaturation(0.4f) }
            m.postConcat(android.graphics.ColorMatrix().apply { setScale(0.55f, 0.55f, 0.55f, 1f) })
            colorFilter = android.graphics.ColorMatrixColorFilter(m)
            if (android.os.Build.VERSION.SDK_INT >= 31) setRenderEffect(android.graphics.RenderEffect.createBlurEffect(dp(14).toFloat(), dp(14).toFloat(), android.graphics.Shader.TileMode.CLAMP))
            scaleX = 1.15f; scaleY = 1.15f
        }
        page.addView(sleepImg, FrameLayout.LayoutParams(-1, -1))
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; gravity = Gravity.CENTER_HORIZONTAL }
        sleepBtn = FrameLayout(this).apply { background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(0x38FFFFFF) } }
        sleepIcon = IconView(this, "power", Color.WHITE, 1.05f)
        sleepBtn.addView(sleepIcon, FrameLayout.LayoutParams(dp(24), dp(24), Gravity.CENTER))
        sleepSpin = android.widget.ProgressBar(this).apply { isIndeterminate = true; indeterminateTintList = android.content.res.ColorStateList.valueOf(Color.WHITE); visibility = View.GONE }
        sleepBtn.addView(sleepSpin, FrameLayout.LayoutParams(dp(30), dp(30), Gravity.CENTER))
        col.addView(sleepBtn, LinearLayout.LayoutParams(dp(48), dp(48)))
        sleepText = TextView(this).apply { text = "摄像头已关闭，点按开启"; textSize = 13f; setTextColor(0xD9FFFFFF.toInt()); setShadowLayer(6f, 0f, 0f, 0x66000000); setPadding(0, dp(10), 0, 0) }
        sleepText.isSingleLine = true
        col.addView(sleepText, LinearLayout.LayoutParams(-2, -2))
        page.addView(col, FrameLayout.LayoutParams(-2, -2, Gravity.CENTER))
        return page
    }

    private fun sleepBusy(busy: Boolean) {
        sleepIcon.visibility = if (busy) View.GONE else View.VISIBLE
        sleepSpin.visibility = if (busy) View.VISIBLE else View.GONE
        sleepBtn.background = GradientDrawable().apply { shape = GradientDrawable.OVAL; setColor(if (busy) 0 else 0x38FFFFFF) }
        sleepText.text = when {
            checkingPower -> "正在确认摄像头状态 · 非实时画面"
            busy -> "正在开启…"
            cameraOffline -> "无法连接摄像头 · 点按重试"
            else -> "摄像头已关闭，点按开启"
        }
    }

    /** The last picture, from the phone's copy first (the home page keeps one), then fresh from the hub. */
    private fun loadSleepPicture() {
        val f = java.io.File(filesDir, "campics/latest.jpg")
        if (f.exists()) BitmapFactory.decodeFile(f.path)?.let { sleepImg.setImageBitmap(soft(it)) }
        scope.launch { bitmap("/api/cam/file/latest.jpg")?.let { sleepImg.setImageBitmap(soft(it)) } }
    }

    /** Before Android 12 there is no blur effect: a tiny copy drawn large is just as soft. */
    private fun soft(b: Bitmap): Bitmap =
        if (android.os.Build.VERSION.SDK_INT >= 31) b else Bitmap.createScaledBitmap(b, 32, maxOf(1, 32 * b.height / maxOf(1, b.width)), true)

    private fun showSleep(animate: Boolean) {
        sleepBusy(false)
        loadSleepPicture()
        sleepPage.visibility = View.VISIBLE
        sleepPage.animate().cancel()
        if (animate) sleepPage.animate().alpha(1f).setDuration(700).start() else sleepPage.alpha = 1f
        barJob?.cancel(); bar.visibility = View.INVISIBLE
    }

    private fun hideSleep() {
        sleepPage.animate().cancel(); sleepPage.alpha = 0f; sleepPage.visibility = View.GONE
        sleepBusy(false); showBar()
    }

    private suspend fun refreshCamera() {
        val wasPowered = powered
        val r = withTimeoutOrNull(6500) { call("GET", "/api/cam/power") }
        checkingPower = false
        cameraOffline = r == null || !r.optBoolean("online", true) || r.isNull("on")
        powered = !cameraOffline && r?.optBoolean("on", false) == true
        if (!powered) {
            stopPicture(); shown = false; loading.done(); showSleep(false)
        } else {
            hideSleep()
            if (!wasPowered) playLive()
        }
    }

    private fun togglePower() {
        if (powerBusy || checkingPower) return
        powerBusy = true; powerBtn.isEnabled = false
        scope.launch {
            try {
                if (cameraOffline) {
                    checkingPower = true; sleepBusy(true); refreshCamera()
                    return@launch
                }
                val want = !powered
                sleepBusy(true)
                val r = call("POST", "/api/cam/power?on=" + if (want) "1" else "0")
                if (r == null || r.isNull("on") || r.optBoolean("on") != want) {
                    toast("摄像头未确认操作，请重试"); refreshCamera(); return@launch
                }
                cameraOffline = false; powered = want
                if (want) { hideSleep(); playLive() }
                else { stopPicture(); shown = false; loading.done(); showSleep(true) }
            } finally { powerBusy = false; powerBtn.isEnabled = true; sleepBusy(false) }
        }
    }

    private fun showBar() {
        if (sleepPage.visibility == View.VISIBLE && !powered) return      // the off page has its own button, nothing else
        bar.animate().cancel(); bar.visibility = View.VISIBLE; bar.alpha = 1f
        barJob?.cancel()
        barJob = scope.launch { delay(4500); if (powered && !talking) hideBar() }
    }

    private fun hideBar() { bar.animate().alpha(0f).setDuration(250).withEndAction { bar.visibility = View.INVISIBLE }.start() }

    private fun snapshot() {
        if (!powered || cameraOffline) { toast("摄像头未连接"); return }
        scope.launch {
            val bmp = bitmap("/cam/api/frame.jpeg?src=$src")
            if (bmp == null) { toast("截图失败"); return@launch }
            withContext(Dispatchers.IO) {
                val v = ContentValues().apply {
                    put(MediaStore.Images.Media.DISPLAY_NAME, "camera_${SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())}.jpg")
                    put(MediaStore.Images.Media.MIME_TYPE, "image/jpeg")
                    put(MediaStore.Images.Media.RELATIVE_PATH, "Pictures/HomeHub")
                }
                contentResolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, v)?.let { u ->
                    contentResolver.openOutputStream(u)?.use { bmp.compress(Bitmap.CompressFormat.JPEG, 92, it) }
                }
            }
            toast("已存到相册")
        }
    }

    // ---- recording: the hub's live stream is a fragmented MP4, so what it sends is already a playable file ------------------
    private var recJob: Job? = null
    @Volatile private var recording = false

    private fun toggleRecord() {
        if (recJob == null && (!powered || cameraOffline)) { toast("摄像头未连接"); return }
        if (recJob != null) { recording = false; return }
        recording = true
        recJob = scope.launch {
            val name = "camera_${SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(Date())}.mp4"
            val values = ContentValues().apply {
                put(MediaStore.Video.Media.DISPLAY_NAME, name); put(MediaStore.Video.Media.MIME_TYPE, "video/mp4")
                put(MediaStore.Video.Media.RELATIVE_PATH, "Movies/HomeHub"); put(MediaStore.Video.Media.IS_PENDING, 1)
            }
            val uri = contentResolver.insert(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, values)
            if (uri == null) { toast("录像没法保存"); recJob = null; recording = false; return@launch }
            recBtn.set("record", Color.parseColor("#E5484D"))
            val t0 = System.currentTimeMillis()
            val tick = launch { while (true) { val s = (System.currentTimeMillis() - t0) / 1000; recLabel.text = "● %d:%02d".format(s / 60, s % 60); delay(500) } }
            try {
                withContext(Dispatchers.IO) {
                    val req = Request.Builder().url("$base/api/cam/live").header("Authorization", "Bearer ${BuildConfig.TOKEN}").build()
                    val client = Net.client.newBuilder().readTimeout(0, TimeUnit.SECONDS).build()
                    client.newCall(req).execute().use { r ->
                        val body = r.body ?: return@use
                        contentResolver.openOutputStream(uri)?.use { out ->
                            val buf = ByteArray(64 * 1024)
                            val input = body.byteStream()
                            while (recording && System.currentTimeMillis() - t0 < 10 * 60_000) {
                                val n = input.read(buf); if (n < 0) break
                                out.write(buf, 0, n)
                            }
                        }
                    }
                }
            } catch (e: Exception) { /* dropped: keep what was written */ }
            tick.cancel()
            contentResolver.update(uri, ContentValues().apply { put(MediaStore.Video.Media.IS_PENDING, 0) }, null, null)
            recBtn.set("record", ink); recLabel.text = ""; recJob = null; recording = false
            toast("录像已存到相册（Movies/HomeHub）")
        }
    }

    // ---- talking: tap to start, tap again to stop. The microphone goes to the hub as raw PCM and from there to the camera's speaker;
    // the microphone icon fills with colour from the bottom as the voice gets louder.
    @Volatile private var talking = false

    private fun toggleTalk() { if (!talking && (!powered || cameraOffline)) { toast("摄像头未连接"); return }; if (talking) stopTalk() else startTalk() }

    private fun setMicLevel(v: Float) { talkTool.level = v; fullMic.level = v; pillMic.level = v; talkTool.invalidate(); fullMic.invalidate(); pillMic.invalidate() }

    private fun startTalk() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 7); toast("请允许使用麦克风，再点一次说话"); return
        }
        if (talking) return
        talking = true; applyVolume(); talkHint.visibility = View.VISIBLE; talkLabel.text = "说话中…"; showBar()
        scope.launch(Dispatchers.IO) {
            val minBuf = AudioRecord.getMinBufferSize(16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
            val rec = AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, 16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, 1920))
            val body = object : RequestBody() {
                override fun contentType() = "application/octet-stream".toMediaType()
                override fun isOneShot() = true
                override fun writeTo(sink: BufferedSink) {
                    rec.startRecording()
                    val buf = ByteArray(640)      // 20 ms at a time: what waits in here is delay at the camera's speaker
                    var smooth = 0f
                    try {
                        while (talking) {
                            val n = rec.read(buf, 0, buf.size)
                            if (n > 0) {
                                sink.write(buf, 0, n); sink.flush()
                                var sum = 0.0; var i = 0
                                while (i + 1 < n) { val v = (buf[i].toInt() and 0xFF) or (buf[i + 1].toInt() shl 8); sum += v.toShort().toDouble() * v.toShort(); i += 2 }
                                val rms = Math.sqrt(sum / (n / 2))
                                val target = Math.sqrt(rms / 2500.0).toFloat().coerceIn(0f, 1f)      // square root: quiet voices already move it
                                smooth = if (target > smooth) target else smooth * 0.7f + target * 0.3f      // jumps up with the voice, falls back gently
                                runOnUiThread { setMicLevel(smooth) }
                            } else if (n < 0) break
                        }
                    } finally { rec.stop(); rec.release() }
                }
            }
            try {
                val client = Net.client.newBuilder().readTimeout(0, TimeUnit.SECONDS).writeTimeout(0, TimeUnit.SECONDS).callTimeout(0, TimeUnit.SECONDS).build()
                client.newCall(Request.Builder().url("$base/api/cam/talk").header("Authorization", "Bearer ${BuildConfig.TOKEN}").post(body).build()).execute().use { r ->
                    if (!r.isSuccessful) withContext(Dispatchers.Main) { toast("对讲没接通（${r.code}）") }
                }
            } catch (e: Exception) { /* the hub ended it */ }
            withContext(Dispatchers.Main) { talking = false; talkHint.visibility = View.GONE; talkLabel.text = "点击说话"; setMicLevel(0f); applyVolume(); showBar() }
        }
    }

    private fun stopTalk() { talking = false; talkHint.visibility = View.GONE; talkLabel.text = "点击说话"; setMicLevel(0f); applyVolume() }

    // ---- settings: the camera's own switches (through the hub's direct link) and the hub's movement recording ------------------
    private fun showSettings() {
        val sh = HhSheet(this, "摄像头设置")
        val wait = Hh.text(this, "读取中…", 14f, Hh.sub).apply { gravity = Gravity.CENTER; setPadding(0, dp(18), 0, dp(18)) }
        sh.body.addView(wait, LinearLayout.LayoutParams(-1, -2))
        sh.show()
        scope.launch {
            val s = call("GET", "/api/cam/settings")
            sh.body.removeView(wait)
            if (s == null) { sh.body.addView(Hh.text(this@CameraActivity, "读不到摄像头的设置（摄像头关着，或不在家里的网络里）", 14f, Hh.sub).apply { gravity = Gravity.CENTER; setPadding(0, dp(12), 0, dp(12)) }); return@launch }
            fun post(key: String, value: Any) { scope.launch { if (call("POST", "/api/cam/settings", JSONObject().put("key", key).put("value", value)) == null) toast("没成功") } }
            fun sw(p: LinearLayout, label: String, key: String, note: String? = null, checked: Boolean = s.optBoolean(key, false), on: Any = true, off: Any = false) {
                lateinit var t: HhSwitch
                t = HhSwitch(this@CameraActivity, checked) { v -> post(key, if (v) on else off) }
                sh.row(p, label, note, t) { t.performClick() }
            }
            sh.group("画面")
            var p = sh.panel()
            sh.chips(p, "夜视", listOf("自动" to 2, "开启" to 1, "关闭" to 0), s.opt("night")) { post("night", it) }
            sw(p, "全彩夜视", "fullcolor")
            sw(p, "画面旋转 180°", "flip", checked = s.optInt("flip") == 180, on = 180, off = 0)
            sw(p, "时间水印", "watermark")
            sw(p, "指示灯", "light")
            sh.group("追踪与识别")
            p = sh.panel()
            sw(p, "人形追踪", "track", "镜头跟着人转")
            sw(p, "宠物追踪", "pettrack")
            sw(p, "人形检测", "human")
            sw(p, "宠物检测", "pet")
            sw(p, "声音检测", "sound")
            sw(p, "哭声检测", "cry")
            sh.group("声音")
            p = sh.panel()
            p.addView(Hh.text(this@CameraActivity, "喇叭音量（对讲时用）", 15f).apply { setPadding(0, dp(14), 0, dp(4)) })
            p.addView(SeekBar(this@CameraActivity).apply {
                max = 100; progress = s.optInt("volume", 100); setPadding(dp(4), dp(8), dp(4), dp(14))
                progressTintList = android.content.res.ColorStateList.valueOf(Hh.blue); thumbTintList = progressTintList
                progressBackgroundTintList = android.content.res.ColorStateList.valueOf(Hh.off)
                setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                    override fun onProgressChanged(sb: SeekBar?, v: Int, u: Boolean) {}
                    override fun onStartTrackingTouch(sb: SeekBar?) {}
                    override fun onStopTrackingTouch(sb: SeekBar?) { post("volume", sb?.progress ?: 100) }
                })
            }, LinearLayout.LayoutParams(-1, -2))
            sh.group("HomeHub 移动记录")
            p = sh.panel()
            sw(p, "记录移动画面", "record", "有人或宠物走动时存一段")
            sh.chips(p, "灵敏度", listOf("低" to "low", "中" to "mid", "高" to "high"), s.optString("sens")) { post("sens", it) }
            sh.chips(p, "每段最长", listOf("10 秒" to 10, "15 秒" to 15, "30 秒" to 30, "60 秒" to 60), s.optDouble("clip_max").toInt()) { post("clip_max", it) }
        }
    }

    private fun qualityName(q: String) = when (q) { "sd" -> "480P"; "max" -> "4K"; else -> "1080P" }

    private fun pickQuality(@Suppress("UNUSED_PARAMETER") anchor: View) {
        val sh = HhSheet(this, "清晰度")
        val p = sh.panel()
        listOf(Triple("max", "4K 超清", "3840×2160，最清楚，要好的网络"), Triple("hd", "1080P 高清", "1920×1080，平时用这个"), Triple("sd", "480P 流畅", "848×480，在外面网络差时用"))
            .forEach { (k, name, note) ->
                val mark = IconView(this, "check", Hh.blue, 1.2f).apply { layoutParams = LinearLayout.LayoutParams(dp(22), dp(22)); visibility = if (k == quality) View.VISIBLE else View.INVISIBLE }
                sh.row(p, name, note, mark) {
                    sh.dismiss()
                    if (k != quality) {
                        quality = k; qualityChip.text = qualityName(k)
                        scope.launch {
                            stopPicture(); loading.start()
                            if (call("POST", "/api/cam/settings", JSONObject().put("key", "quality").put("value", k)) == null) { toast("没成功"); return@launch }
                            delay(6000)                       // the hub lets go of the camera and asks again at the new quality
                            if (powered && live) playLive()
                        }
                    }
                }
            }
        sh.show()
    }

    private fun paintCell(c: TextView, on: Boolean) {
        c.background = round(if (on) teal else Color.parseColor("#F0F0F2"), 10)
        c.setTextColor(if (on) Color.WHITE else ink)
    }

    private fun toggleFull() {
        full = !full
        val ctl = WindowInsetsControllerCompat(window, window.decorView)
        if (full) {
            requestedOrientation = android.content.pm.ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
            WindowCompat.setDecorFitsSystemWindows(window, false)
            ctl.hide(WindowInsetsCompat.Type.systemBars()); ctl.systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            header.visibility = View.GONE
            scroll.visibility = View.GONE
            box.layoutParams = LinearLayout.LayoutParams(-1, -1)
            fullPad.visibility = View.VISIBLE; fullTools.visibility = View.VISIBLE
        } else {
            requestedOrientation = android.content.pm.ActivityInfo.SCREEN_ORIENTATION_PORTRAIT
            WindowCompat.setDecorFitsSystemWindows(window, true)
            ctl.show(WindowInsetsCompat.Type.systemBars())
            header.visibility = View.VISIBLE
            scroll.visibility = View.VISIBLE
            box.layoutParams = LinearLayout.LayoutParams(-1, dp(210))
            fullPad.visibility = View.GONE; fullTools.visibility = View.GONE
        }
        box.postDelayed({ fit() }, 350)
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() { if (full) toggleFull() else super.onBackPressed() }

    // ---- head ----------------------------------------------------------------------------------------------------------------
    private var holdJob: Job? = null
    private var holdDir: String? = null

    /** dir = left/right/up/down while the finger is on the pad, null when it lifts. The hub keeps turning for a couple of seconds
     *  after each command and is told again every second, so a lost request does not leave the camera running. */
    private fun moveHead(dir: String?) {
        if (dir != null && (!powered || cameraOffline || checkingPower)) return
        if (dir == holdDir) return
        holdDir = dir
        holdJob?.cancel()
        if (dir == null) { scope.launch { call("POST", "/api/cam/move?dir=stop&src=$src") }; return }
        holdJob = scope.launch { while (true) { call("POST", "/api/cam/move?dir=$dir&src=$src"); delay(1000) } }
    }

    private suspend fun call(method: String, path: String, json: JSONObject? = null): JSONObject? {
        val b = Request.Builder().url("${base.ifEmpty { Net.current() }}$path")
            .header("Authorization", "Bearer ${BuildConfig.TOKEN}")
            .header("User-Agent", "HomeHub/${BuildConfig.VERSION_NAME} camera")
        val req = if (method == "POST") b.post((json?.toString() ?: "").toRequestBody("application/json".toMediaType())).build() else b.build()
        return CameraHttp.json(req)
    }

    override fun onDestroy() {
        talking = false; recording = false
        scope.cancel()
        player?.release(); player = null
        livePlayer?.stop(); livePlayer = null; liveSurface?.release(); liveSurface = null
        if (holdDir != null) Thread {
            try { Net.client.newCall(Request.Builder().url("${Net.current()}/api/cam/move?dir=stop&src=$src").header("Authorization", "Bearer ${BuildConfig.TOKEN}").post(ByteArray(0).toRequestBody()).build()).execute().close() } catch (e: Exception) { }
        }.start()
        super.onDestroy()
    }

    /** The round pad with four dots. A finger on it picks the side it is nearest to (left/right/up/down); lifting stops. */
    private class Wheel(ctx: Activity, dark: Boolean, private val onDir: (String?) -> Unit) : View(ctx) {
        private val fill = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = if (dark) 0x55000000 else Color.parseColor("#F3F3F5") }
        private val dot = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = if (dark) Color.WHITE else Color.parseColor("#777777") }
        private val hot = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.parseColor("#2BB8B0") }
        private var cur: String? = null

        override fun onDraw(c: Canvas) {
            val cx = width / 2f; val cy = height / 2f; val r = min(width, height) / 2f
            c.drawCircle(cx, cy, r, fill)
            val d = r * 0.72f; val s = r * 0.04f
            fun p(name: String, x: Float, y: Float) = c.drawCircle(x, y, if (cur == name) s * 2.2f else s, if (cur == name) hot else dot)
            p("up", cx, cy - d); p("down", cx, cy + d); p("left", cx - d, cy); p("right", cx + d, cy)
        }

        override fun onTouchEvent(e: MotionEvent): Boolean {
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN, MotionEvent.ACTION_MOVE -> {
                    val dx = e.x - width / 2f; val dy = e.y - height / 2f
                    val dir = if (kotlin.math.hypot(dx, dy) < width * 0.12f) null
                    else if (abs(dx) > abs(dy)) (if (dx < 0) "left" else "right") else (if (dy < 0) "up" else "down")
                    parent.requestDisallowInterceptTouchEvent(true)
                    if (dir != cur) { cur = dir; invalidate(); onDir(dir) }
                }
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> { cur = null; invalidate(); onDir(null) }
            }
            return true
        }
    }
}
