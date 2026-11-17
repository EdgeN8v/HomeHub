package com.homehub.app

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.media.MediaCodec
import android.media.MediaFormat
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.view.Surface
import okhttp3.Call
import okhttp3.Request
import java.io.BufferedInputStream
import java.io.DataInputStream
import java.io.EOFException
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.TimeUnit
import kotlin.math.abs

/**
 * The live picture the way Mijia shows it: no general-purpose player and no playback buffer. The hub's live stream (fragmented MP4, one
 * frame per fragment) is read here, each H.265 frame goes straight to the phone's hardware decoder and is shown the moment it is
 * decoded, at the camera's own frame rhythm. If frames pile up (the fast start hands over everything since the last keyframe, or the
 * network held some back) the old ones are decoded but not shown, so the picture is always within about a third of a second of live
 * and never waits for a buffer to fill. Sound (Opus) is decoded only while it is switched on.
 */
class LivePlayer(private val listener: Listener) {
    interface Listener {
        fun onFirstFrame() {}
        fun onVideoSize(w: Int, h: Int) {}
        /** No new frame for a while (true) / frames again (false). */
        fun onStall(stalled: Boolean) {}
        /** The stream ended or could not be opened; the caller decides whether to try again. */
        fun onError(msg: String) {}
    }

    @Volatile var muted = true
        set(v) { field = v; if (v) audioTrack?.let { runCatching { it.pause(); it.flush() } } }

    @Volatile private var running = false
    private var reader: Thread? = null
    @Volatile private var call: Call? = null
    private val main = Handler(Looper.getMainLooper())

    // video
    @Volatile private var vdec: MediaCodec? = null
    private var outThread: Thread? = null
    @Volatile private var newestPts = 0L                  // newest frame handed to the decoder (µs)
    @Volatile private var lastShown = 0L                  // when a frame was last put on screen (ms)
    private var needKey = true
    private var shownFirst = false
    private var stalled = false

    // sound
    private var adec: MediaCodec? = null
    @Volatile private var audioTrack: AudioTrack? = null
    private var aChannels = 2

    fun start(url: String, surface: Surface) {
        stop()
        running = true
        shownFirst = false; stalled = false; needKey = true; newestPts = 0; lastShown = 0
        reader = Thread({ read(url, surface) }, "live-read").apply { start() }
        main.postDelayed(watch, 500)
    }

    /** Stops and lets go of the decoder (and so of the surface) before returning, so the surface can be used again at once. */
    fun stop() {
        running = false
        main.removeCallbacks(watch)
        call?.cancel()
        reader?.let { if (it !== Thread.currentThread()) it.join(1500) }
        reader = null
    }

    private val watch = object : Runnable {
        override fun run() {
            if (!running) return
            val quiet = shownFirst && System.currentTimeMillis() - lastShown > 2500
            if (quiet != stalled) { stalled = quiet; listener.onStall(quiet) }
            main.postDelayed(this, 500)
        }
    }

    // ---- reading the stream --------------------------------------------------------------------------------------------------
    private class Trk(val id: Int, var scale: Long = 90000, var kind: String = "", var entry: String = "", var w: Int = 0, var h: Int = 0, var channels: Int = 2)

    private fun read(url: String, surface: Surface) {
        val tracks = HashMap<Int, Trk>()
        try {
            val client = Net.client.newBuilder().readTimeout(10, TimeUnit.SECONDS).callTimeout(0, TimeUnit.SECONDS).build()
            val c = client.newCall(Request.Builder().url(url).header("Authorization", "Bearer ${BuildConfig.TOKEN}").build())
            call = c
            c.execute().use { resp ->
                if (!resp.isSuccessful) { fail(if (resp.code == 503) "摄像头没连上" else "服务器回了 ${resp.code}"); return }
                val inp = DataInputStream(BufferedInputStream(resp.body!!.byteStream(), 1 shl 16))
                var frag: Frag? = null
                while (running) {
                    var size = inp.readInt().toLong() and 0xFFFFFFFFL
                    val type = inp.readInt()
                    var hdr = 8L
                    if (size == 1L) { size = inp.readLong(); hdr = 16 }
                    val len = (size - hdr).toInt()
                    when (type) {
                        MOOV -> parseMoov(ByteArray(len).also { inp.readFully(it) }, tracks)
                        MOOF -> frag = parseMoof(ByteArray(len).also { inp.readFully(it) })
                        MDAT -> {
                            val data = ByteArray(len).also { inp.readFully(it) }
                            val f = frag ?: continue
                            frag = null
                            val t = tracks[f.track] ?: continue
                            var off = 0
                            var time = f.base
                            for (i in 0 until f.sizes.size) {
                                val n = f.sizes[i]
                                if (off + n > data.size) break
                                val pts = time * 1_000_000L / t.scale
                                if (t.kind == "vide") video(data, off, n, pts, f.key[i], t, surface)
                                else if (t.kind == "soun" && !muted) audio(data, off, n, pts, t)
                                off += n; time += f.durs[i]
                            }
                        }
                        else -> inp.skipFully(len.toLong())
                    }
                }
            }
        } catch (e: EOFException) {
            fail("直播断了")
        } catch (e: Exception) {
            fail(if (e is java.io.IOException) "连不上摄像头" else "播放出错：${e.javaClass.simpleName}")
        } finally {
            call = null
            release()
        }
    }

    private fun DataInputStream.skipFully(n: Long) { var left = n; while (left > 0) { val s = skip(left); if (s <= 0) { read(); left-- } else left -= s } }

    private fun fail(msg: String) { if (running) main.post { if (running) { running = false; listener.onError(msg) } } }

    private fun release() {
        val dec = vdec; vdec = null
        outThread?.interrupt(); outThread?.join(500); outThread = null
        dec?.let { runCatching { it.stop() }; runCatching { it.release() } }
        adec?.let { runCatching { it.stop() }; runCatching { it.release() } }; adec = null
        audioTrack?.let { runCatching { it.stop() }; runCatching { it.release() } }; audioTrack = null
    }

    // ---- MP4 boxes ----------------------------------------------------------------------------------------------------------
    private fun u32(b: ByteArray, o: Int) = ((b[o].toLong() and 0xFF) shl 24) or ((b[o + 1].toLong() and 0xFF) shl 16) or ((b[o + 2].toLong() and 0xFF) shl 8) or (b[o + 3].toLong() and 0xFF)
    private fun u16(b: ByteArray, o: Int) = ((b[o].toInt() and 0xFF) shl 8) or (b[o + 1].toInt() and 0xFF)
    private fun u64(b: ByteArray, o: Int) = (u32(b, o) shl 32) or u32(b, o + 4)

    /** Calls back with (type, body start, body end) for each box in b[from until to]. */
    private fun boxes(b: ByteArray, from: Int, to: Int, cb: (Int, Int, Int) -> Unit) {
        var i = from
        while (i + 8 <= to) {
            val size = u32(b, i).toInt(); val type = u32(b, i + 4).toInt()
            if (size < 8 || i + size > to) return
            cb(type, i + 8, i + size)
            i += size
        }
    }

    private fun parseMoov(b: ByteArray, tracks: HashMap<Int, Trk>) {
        boxes(b, 0, b.size) { type, s, e ->
            if (type != TRAK) return@boxes
            var t: Trk? = null
            fun walk(from: Int, to: Int) {
                boxes(b, from, to) { ty, bs, be ->
                    when (ty) {
                        TKHD -> { val v = b[bs].toInt(); t = Trk(u32(b, bs + if (v == 1) 20 else 12).toInt()) }
                        MDIA, MINF, STBL -> walk(bs, be)
                        MDHD -> { val v = b[bs].toInt(); t?.scale = u32(b, bs + if (v == 1) 20 else 12) }
                        HDLR -> t?.kind = String(b, bs + 8, 4, Charsets.US_ASCII)
                        STSD -> {
                            val es = bs + 8                                   // first sample entry
                            t?.entry = String(b, es + 4, 4, Charsets.US_ASCII)
                            if (t?.kind == "vide") { t?.w = u16(b, es + 32); t?.h = u16(b, es + 34) }
                            if (t?.kind == "soun") t?.channels = u16(b, es + 24)
                        }
                    }
                }
            }
            walk(s, e)
            t?.let { tracks[it.id] = it }
        }
    }

    private class Frag(val track: Int, val base: Long, val sizes: IntArray, val durs: LongArray, val key: BooleanArray)

    private fun parseMoof(b: ByteArray): Frag? {
        var out: Frag? = null
        boxes(b, 0, b.size) { type, s, e ->
            if (type != TRAF) return@boxes
            var track = 0; var defDur = 0L; var defSize = 0; var defFlags = 0L; var base = 0L
            boxes(b, s, e) { ty, bs, _ ->
                when (ty) {
                    TFHD -> {
                        val fl = u32(b, bs).toInt() and 0xFFFFFF
                        track = u32(b, bs + 4).toInt()
                        var o = bs + 8
                        if (fl and 0x01 != 0) o += 8
                        if (fl and 0x02 != 0) o += 4
                        if (fl and 0x08 != 0) { defDur = u32(b, o); o += 4 }
                        if (fl and 0x10 != 0) { defSize = u32(b, o).toInt(); o += 4 }
                        if (fl and 0x20 != 0) { defFlags = u32(b, o) }
                    }
                    TFDT -> base = if (b[bs].toInt() == 1) u64(b, bs + 4) else u32(b, bs + 4)
                    TRUN -> {
                        val fl = u32(b, bs).toInt() and 0xFFFFFF
                        val n = u32(b, bs + 4).toInt().coerceIn(0, 10000)
                        var o = bs + 8
                        if (fl and 0x01 != 0) o += 4
                        var first = -1L
                        if (fl and 0x04 != 0) { first = u32(b, o); o += 4 }
                        val sizes = IntArray(n); val durs = LongArray(n); val key = BooleanArray(n)
                        for (i in 0 until n) {
                            var d = defDur; var sz = defSize; var f = if (i == 0 && first >= 0) first else defFlags
                            if (fl and 0x100 != 0) { d = u32(b, o); o += 4 }
                            if (fl and 0x200 != 0) { sz = u32(b, o).toInt(); o += 4 }
                            if (fl and 0x400 != 0) { f = u32(b, o); o += 4 }
                            if (fl and 0x800 != 0) o += 4
                            sizes[i] = sz; durs[i] = d; key[i] = (f and 0x00010000L) == 0L
                        }
                        out = Frag(track, base, sizes, durs, key)
                    }
                }
            }
        }
        return out
    }

    // ---- video --------------------------------------------------------------------------------------------------------------
    private fun video(data: ByteArray, off: Int, n: Int, pts: Long, key: Boolean, t: Trk, surface: Surface) {
        // length-prefixed NAL units -> start codes (what the decoder takes), and the parameter sets of a keyframe
        val buf = data.copyOfRange(off, off + n)
        val params = java.io.ByteArrayOutputStream()
        var i = 0
        while (i + 4 <= buf.size) {
            val len = u32(buf, i).toInt()
            if (len <= 0 || i + 4 + len > buf.size) break
            buf[i] = 0; buf[i + 1] = 0; buf[i + 2] = 0; buf[i + 3] = 1
            val nalType = if (t.entry.startsWith("av")) buf[i + 4].toInt() and 0x1F else (buf[i + 4].toInt() shr 1) and 0x3F
            val isParam = if (t.entry.startsWith("av")) nalType == 7 || nalType == 8 else nalType in 32..34
            if (isParam) params.write(buf, i, 4 + len)
            i += 4 + len
        }
        if (vdec == null) {
            if (!key || params.size() == 0) return
            val mime = if (t.entry.startsWith("av")) MediaFormat.MIMETYPE_VIDEO_AVC else MediaFormat.MIMETYPE_VIDEO_HEVC
            val fmt = MediaFormat.createVideoFormat(mime, maxOf(t.w, 16), maxOf(t.h, 16)).apply {
                setByteBuffer("csd-0", ByteBuffer.wrap(params.toByteArray()))
                setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 4 shl 20)             // a 4K keyframe can be close to 1 MB
                if (Build.VERSION.SDK_INT >= 30) setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
                setInteger(MediaFormat.KEY_PRIORITY, 0)
            }
            val dec = MediaCodec.createDecoderByType(mime)
            dec.configure(fmt, surface, null, 0)
            dec.start()
            vdec = dec
            needKey = false
            outThread = Thread({ drain(dec) }, "live-out").apply { start() }
            if (t.w > 0) main.post { listener.onVideoSize(t.w, t.h) }
        }
        if (needKey) { if (!key) return; needKey = false }
        val dec = vdec ?: return
        // a decoder that has no room for this frame within 0.3 s is too far behind: start again at the next keyframe
        val idx = dec.dequeueInputBuffer(300_000)
        if (idx < 0) { needKey = true; return }
        val ib = dec.getInputBuffer(idx) ?: return
        ib.clear()
        if (buf.size > ib.capacity()) { dec.queueInputBuffer(idx, 0, 0, pts, 0); needKey = true; return }
        ib.put(buf)
        newestPts = pts
        dec.queueInputBuffer(idx, 0, buf.size, pts, if (key) MediaCodec.BUFFER_FLAG_KEY_FRAME else 0)
    }

    /** Puts decoded frames on screen at the camera's rhythm, as soon as they are decoded; frames well behind live are skipped. */
    private fun drain(dec: MediaCodec) {
        val info = MediaCodec.BufferInfo()
        var anchorWall = 0L; var anchorPts = 0L
        try {
            while (running && !Thread.currentThread().isInterrupted) {
                val idx = dec.dequeueOutputBuffer(info, 20_000)
                if (idx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                    val f = dec.outputFormat
                    var w = f.getInteger(MediaFormat.KEY_WIDTH); var h = f.getInteger(MediaFormat.KEY_HEIGHT)
                    if (f.containsKey("crop-right")) { w = f.getInteger("crop-right") - f.getInteger("crop-left") + 1; h = f.getInteger("crop-bottom") - f.getInteger("crop-top") + 1 }
                    main.post { listener.onVideoSize(w, h) }
                    continue
                }
                if (idx < 0) continue
                val pts = info.presentationTimeUs
                if (newestPts - pts > 350_000) { dec.releaseOutputBuffer(idx, false); anchorWall = 0; continue }      // behind live: decode only
                val now = System.nanoTime()
                var target = anchorWall + (pts - anchorPts) * 1000
                if (anchorWall == 0L || abs(target - now) > 300_000_000L) { anchorWall = now + 40_000_000L; anchorPts = pts; target = anchorWall }
                if (target <= now) dec.releaseOutputBuffer(idx, true) else dec.releaseOutputBuffer(idx, target)
                lastShown = System.currentTimeMillis()
                if (!shownFirst) { shownFirst = true; main.post { if (running) listener.onFirstFrame() } }
            }
        } catch (e: Exception) { /* decoder stopped */ }
    }

    // ---- sound --------------------------------------------------------------------------------------------------------------
    private fun audio(data: ByteArray, off: Int, n: Int, pts: Long, t: Trk) {
        if (t.entry != "Opus") return
        val dec: MediaCodec = adec ?: run {
            aChannels = t.channels.coerceIn(1, 2)
            val head = ByteBuffer.allocate(19).order(ByteOrder.LITTLE_ENDIAN).apply {
                put("OpusHead".toByteArray()); put(1); put(aChannels.toByte()); putShort(0); putInt(48000); putShort(0); put(0); flip()
            }
            fun ns(v: Long) = ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN).putLong(v).apply { flip() }
            val fmt = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, 48000, aChannels).apply {
                setByteBuffer("csd-0", head); setByteBuffer("csd-1", ns(0)); setByteBuffer("csd-2", ns(80_000_000))
            }
            val d = try { MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_AUDIO_OPUS).apply { configure(fmt, null, null, 0); start() } } catch (e: Exception) { return }
            adec = d
            d
        }
        val idx = dec.dequeueInputBuffer(0)
        if (idx >= 0) {
            dec.getInputBuffer(idx)?.let { it.clear(); it.put(data, off, n); dec.queueInputBuffer(idx, 0, n, pts, 0) }
        }
        val info = MediaCodec.BufferInfo()
        while (true) {
            val o = dec.dequeueOutputBuffer(info, 0)
            if (o < 0) break
            val ob = dec.getOutputBuffer(o)
            if (ob != null && info.size > 0 && !muted) {
                val tr = track()
                if (tr.playState != AudioTrack.PLAYSTATE_PLAYING) tr.play()
                ob.position(info.offset); ob.limit(info.offset + info.size)
                tr.write(ob, info.size, AudioTrack.WRITE_NON_BLOCKING)       // no room = late sound: dropped, so it never drifts behind
            }
            dec.releaseOutputBuffer(o, false)
        }
    }

    private fun track(): AudioTrack {
        audioTrack?.let { return it }
        val ch = if (aChannels == 1) AudioFormat.CHANNEL_OUT_MONO else AudioFormat.CHANNEL_OUT_STEREO
        val min = AudioTrack.getMinBufferSize(48000, ch, AudioFormat.ENCODING_PCM_16BIT)
        val t = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_MOVIE).build())
            .setAudioFormat(AudioFormat.Builder().setSampleRate(48000).setChannelMask(ch).setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
            .setBufferSizeInBytes(maxOf(min, 48000 * 2 * aChannels / 4))      // about 0.25 s
            .setTransferMode(AudioTrack.MODE_STREAM)
            .setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY)
            .build()
        audioTrack = t
        return t
    }

    companion object {
        private fun fourcc(s: String) = s.fold(0) { acc, c -> (acc shl 8) or c.code }
        private val MOOV = fourcc("moov"); private val MOOF = fourcc("moof"); private val MDAT = fourcc("mdat")
        private val TRAK = fourcc("trak"); private val TKHD = fourcc("tkhd"); private val MDIA = fourcc("mdia"); private val MINF = fourcc("minf")
        private val STBL = fourcc("stbl"); private val MDHD = fourcc("mdhd"); private val HDLR = fourcc("hdlr"); private val STSD = fourcc("stsd")
        private val TRAF = fourcc("traf"); private val TFHD = fourcc("tfhd"); private val TFDT = fourcc("tfdt"); private val TRUN = fourcc("trun")
    }
}
