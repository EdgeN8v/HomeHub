package com.homehub.app

import android.app.Activity
import android.graphics.Color
import android.graphics.Outline
import android.graphics.drawable.GradientDrawable
import android.view.Gravity
import android.view.TextureView
import android.view.View
import android.view.ViewGroup
import android.view.ViewOutlineProvider
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
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
import kotlin.math.exp

/**
 * "Loading 63%": the number is an estimate (the hub does not know how far the camera is), easing towards 95% and jumping to 100% when
 * the first picture shows - what matters is that a wait looks like progress, as in Mijia.
 */
class LoadingPercent(private val scope: CoroutineScope, private val view: TextView, private val label: String = "加载中") {
    private var job: Job? = null

    fun start() {
        job?.cancel()
        view.visibility = View.VISIBLE
        val t0 = System.currentTimeMillis()
        job = scope.launch {
            while (true) {
                val ms = System.currentTimeMillis() - t0
                view.text = "$label ${(95 * (1 - exp(-ms / 2200.0))).toInt()}%"
                delay(120)
            }
        }
    }

    fun done() { job?.cancel(); job = null; view.visibility = View.GONE }
    fun fail(text: String) { job?.cancel(); job = null; view.visibility = View.VISIBLE; view.text = text }
}

/**
 * The camera picture inside the home page's camera card: a native player laid exactly over the card's picture box (the WebView cannot
 * play the camera's H.265). The page keeps telling it where the card is while it scrolls; taps on the picture reach the page, which
 * shows / hides the buttons (pause, sound, enlarge) - they hide again after two seconds, as in Mijia.
 */
class InlineCam(private val act: Activity, private val root: FrameLayout, private val onEnded: () -> Unit, private val openFull: (String, String) -> Unit) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private val density = act.resources.displayMetrics.density
    private var player: ExoPlayer? = null
    private var frame: FrameLayout? = null
    private var texture: TextureView? = null
    private var status: TextView? = null
    private var loading: LoadingPercent? = null
    private var controls: View? = null
    private var pauseIcon: IconView? = null
    private var muteIcon: IconView? = null
    private var job: Job? = null
    private var hideJob: Job? = null
    private var catchJob: Job? = null
    private var retries = 0
    private var shown = false
    private var stallJob: Job? = null
    private var src = "c700"
    private var clip = ""
    private var muted = true
    private var paused = false

    private fun dp(v: Int) = (v * density).toInt()
    private fun px(v: Float) = (v * density).toInt()

    fun playing() = frame != null

    private fun iconButton(kind: String, size: Int, onClick: () -> Unit): IconView = IconView(act, kind, Color.WHITE, 1f, true).apply {
        setPadding(dp(8), dp(8), dp(8), dp(8)); isClickable = true; setOnClickListener { onClick(); showControls() }
        layoutParams = LinearLayout.LayoutParams(dp(size), dp(size)).apply { rightMargin = dp(4) }
    }

    fun play(src: String, clip: String, x: Float, y: Float, w: Float, h: Float, name: String, radius: Float) {
        stop(notify = false)
        this.src = src; this.clip = clip; paused = false
        val r = radius * density
        val f = FrameLayout(act).apply {
            setBackgroundColor(Color.BLACK)
            clipToOutline = true
            // only the top corners are round (the card's): the outline reaches below the box so the bottom ones fall outside it
            outlineProvider = object : ViewOutlineProvider() { override fun getOutline(v: View, o: Outline) { o.setRoundRect(0, 0, v.width, v.height + r.toInt(), r) } }
        }
        val tv = TextureView(act)
        f.addView(tv, FrameLayout.LayoutParams(-1, -1, Gravity.CENTER))
        val st = TextView(act).apply { setTextColor(Color.WHITE); textSize = 14f; setShadowLayer(4f, 0f, 0f, Color.BLACK) }
        f.addView(st, FrameLayout.LayoutParams(-2, -2, Gravity.CENTER))
        loading = LoadingPercent(scope, st); loading?.start()

        // top: name and 进入; bottom: pause, sound, enlarge. Not clickable themselves, so a tap beside the buttons reaches the page.
        val bar = FrameLayout(act)
        val top = LinearLayout(act).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL; setPadding(dp(14), dp(10), dp(8), dp(18))
            background = GradientDrawable(GradientDrawable.Orientation.TOP_BOTTOM, intArrayOf(0x59000000, 0x00000000))
            addView(TextView(act).apply { text = name; setTextColor(Color.WHITE); textSize = 16f; setTypeface(null, android.graphics.Typeface.BOLD) }, LinearLayout.LayoutParams(0, -2, 1f))
            addView(TextView(act).apply { text = "进入 ›"; setTextColor(Color.WHITE); textSize = 14f; setPadding(dp(8), dp(4), dp(8), dp(4)); isClickable = true
                setOnClickListener { val c = this@InlineCam.clip; val s = this@InlineCam.src; CamHandoff.put(texture?.bitmap); stop(notify = true); openFull(s, c) } })
        }
        bar.addView(top, FrameLayout.LayoutParams(-1, -2, Gravity.TOP))
        val bottom = LinearLayout(act).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL; setPadding(dp(12), dp(14), dp(8), dp(12))
        }
        val pi = iconButton("pause", 36) { togglePause() }; pauseIcon = pi
        val mi = iconButton("mute", 36) { toggleMute() }; muteIcon = mi
        bottom.addView(pi); bottom.addView(View(act), LinearLayout.LayoutParams(0, 1, 1f)); bottom.addView(mi)
        bottom.addView(iconButton("expand", 36) { val c = this.clip; val s = this.src; CamHandoff.put(texture?.bitmap); stop(notify = true); openFull(s, c) })
        bar.addView(bottom, FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM))
        controls = bar
        f.addView(bar, FrameLayout.LayoutParams(-1, -1))
        frame = f; texture = tv; status = st
        root.addView(f, FrameLayout.LayoutParams(px(w), px(h)).apply { gravity = Gravity.TOP or Gravity.START; leftMargin = px(x); topMargin = px(y) })
        showControls()

        if (clip.isEmpty()) startLive(tv) else startClip(tv)
    }

    // ---- live: frames straight to the hardware decoder (LivePlayer), no playback buffer
    private var live: LivePlayer? = null
    private var liveSurface: android.view.Surface? = null

    private fun startLive(tv: TextureView) {
        val lp = LivePlayer(object : LivePlayer.Listener {
            override fun onFirstFrame() { shown = true; retries = 0; loading?.done() }
            override fun onVideoSize(w: Int, h: Int) { fit(VideoSize(w, h)) }
            override fun onStall(stalled: Boolean) { if (stalled) loading?.start() else loading?.done() }
            // the camera may be waking up or the hub reconnecting - keep trying quietly instead of showing an error
            override fun onError(msg: String) {
                if (frame == null || paused) return
                if (retries < 8) { retries++; if (!shown) loading?.start(); job = scope.launch { delay(1500); if (frame != null && !paused) liveGo() } }
                else loading?.fail("摄像头没连上，点一下再试")
            }
        })
        lp.muted = muted
        live = lp
        withSurface(tv) { liveSurface = it; liveGo() }
    }

    private fun liveGo() {
        val lp = live ?: return; val sf = liveSurface ?: return
        job = scope.launch {
            val base = try { Net.baseUrl() } catch (e: Exception) { loading?.fail("连不上服务器"); return@launch }
            lp.start("$base/api/cam/live", sf)
        }
    }

    private fun withSurface(tv: TextureView, use: (android.view.Surface) -> Unit) {
        tv.surfaceTexture?.let { use(android.view.Surface(it)); return }
        tv.surfaceTextureListener = object : TextureView.SurfaceTextureListener {
            override fun onSurfaceTextureAvailable(st: android.graphics.SurfaceTexture, w: Int, h: Int) { use(android.view.Surface(st)) }
            override fun onSurfaceTextureSizeChanged(st: android.graphics.SurfaceTexture, w: Int, h: Int) {}
            override fun onSurfaceTextureDestroyed(st: android.graphics.SurfaceTexture): Boolean { live?.stop(); return true }
            override fun onSurfaceTextureUpdated(st: android.graphics.SurfaceTexture) {}
        }
    }

    // ---- a recorded clip: an ordinary file, ExoPlayer
    private fun startClip(tv: TextureView) {
        val http = OkHttpDataSource.Factory(Net.client).setDefaultRequestProperties(mapOf("Authorization" to "Bearer ${BuildConfig.TOKEN}"))
        val p = ExoPlayer.Builder(act).setMediaSourceFactory(DefaultMediaSourceFactory(http)).build()
        player = p
        p.volume = if (muted) 0f else 1f
        p.setVideoTextureView(tv)
        p.addListener(object : Player.Listener {
            override fun onVideoSizeChanged(size: VideoSize) { fit(size) }
            override fun onRenderedFirstFrame() { shown = true; loading?.done() }
            override fun onPlaybackStateChanged(state: Int) {
                if (state == Player.STATE_READY) loading?.done()
                if (state == Player.STATE_ENDED) stop(notify = true)
            }
            override fun onPlayerError(error: PlaybackException) { loading?.fail("这段录像放不了") }
        })
        p.playWhenReady = true
        job = scope.launch { prepare() }
    }

    private suspend fun prepare() {
        val p = player ?: return
        val base = try { Net.baseUrl() } catch (e: Exception) { loading?.fail("连不上服务器"); return }
        val url = if (clip.isEmpty()) "$base/api/cam/live" else "$base/api/cam/file/$clip"
        p.setMediaItem(MediaItem.fromUri(url)); p.prepare(); p.playWhenReady = true
    }

    private fun togglePause() {
        paused = !paused
        pauseIcon?.set(if (paused) "play" else "pause")
        if (clip.isEmpty()) { if (paused) live?.stop() else { retries = 0; liveGo() } }      // live: pick the picture up again from now
        else player?.let { if (paused) it.pause() else it.play() }
    }

    private fun toggleMute() { muted = !muted; player?.volume = if (muted) 0f else 1f; live?.muted = muted; muteIcon?.set(if (muted) "mute" else "volume") }

    /** A tap on the picture (the page reports it): show the buttons, they hide again after two seconds unless paused. */
    fun tap() { if (controls?.visibility == View.VISIBLE) hideControls() else showControls() }

    private fun showControls() {
        controls?.visibility = View.VISIBLE
        hideJob?.cancel()
        hideJob = scope.launch { delay(4500); if (!paused) hideControls() }
    }

    private fun hideControls() { controls?.visibility = View.INVISIBLE }

    private fun fit(size: VideoSize) {
        val f = frame ?: return; val tv = texture ?: return
        if (size.width == 0 || size.height == 0 || f.width == 0) return
        val ratio = size.width * size.pixelWidthHeightRatio / size.height
        var vw = f.width; var vh = (vw / ratio).toInt()
        if (vh > f.height) { vh = f.height; vw = (vh * ratio).toInt() }
        if (kotlin.math.abs(vw - f.width) < f.width * 0.04 && kotlin.math.abs(vh - f.height) < f.height * 0.04) { vw = f.width; vh = f.height }   // the card box is 16:9 like the picture: fill it, no thin black edge
        tv.layoutParams = FrameLayout.LayoutParams(vw, vh, Gravity.CENTER)
        tv.scaleX = 1.035f; tv.scaleY = 1.035f          // the stream has a thin black rim of its own: crop it off
    }

    fun move(x: Float, y: Float, w: Float, h: Float, visible: Boolean) {
        val f = frame ?: return
        val lp = f.layoutParams as FrameLayout.LayoutParams
        val nw = px(w); val nh = px(h); val nl = px(x); val nt = px(y)
        if (lp.width != nw || lp.height != nh || lp.leftMargin != nl || lp.topMargin != nt) {
            lp.width = nw; lp.height = nh; lp.leftMargin = nl; lp.topMargin = nt
            f.layoutParams = lp
        }
        f.visibility = if (visible) View.VISIBLE else View.INVISIBLE
        player?.volume = if (muted || !visible) 0f else 1f
        live?.muted = muted || !visible
    }

    fun stop(notify: Boolean = false) {
        job?.cancel(); job = null; hideJob?.cancel(); hideJob = null; catchJob?.cancel(); catchJob = null
        loading?.done(); loading = null
        player?.release(); player = null
        live?.stop(); live = null; liveSurface?.release(); liveSurface = null; shown = false; retries = 0
        frame?.let { (it.parent as? ViewGroup)?.removeView(it) }
        frame = null; texture = null; status = null; controls = null
        if (notify) onEnded()
    }

    fun destroy() { stop(); scope.cancel() }
}


/** The last frame the card showed, picked up by the full camera page so it opens on that picture instead of black. */
object CamHandoff {
    private var frame: android.graphics.Bitmap? = null
    private var at = 0L
    fun put(b: android.graphics.Bitmap?) { frame = b; at = System.currentTimeMillis() }
    fun take(): android.graphics.Bitmap? { val b = frame.takeIf { System.currentTimeMillis() - at < 5000 }; frame = null; return b }
}
