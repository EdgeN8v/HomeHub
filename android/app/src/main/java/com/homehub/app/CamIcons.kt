package com.homehub.app

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RectF
import android.view.View
import kotlin.math.min

/**
 * The camera page's icons: one family - 24-unit grid, the same thin round stroke, no filled shapes, no hard corners - drawn in code so
 * they scale to any size and take any colour. [level] (0..1) colours the icon from the bottom up in [accent], used by the microphone to
 * show how loud the voice is.
 */
class IconView(ctx: Context, var kind: String, var color: Int = Color.WHITE, private val weight: Float = 1f, private val halo: Boolean = false) : View(ctx) {
    var level = 0f
    var accent = Color.parseColor("#2BB8B0")
    private val p = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeCap = Paint.Cap.ROUND; strokeJoin = Paint.Join.ROUND }

    fun set(kind: String, color: Int = this.color) { this.kind = kind; this.color = color; invalidate() }

    override fun onDraw(c: Canvas) {
        val u = min(width, height) / 24f
        val ox = (width - 24f * u) / 2f; val oy = (height - 24f * u) / 2f
        c.save(); c.translate(ox, oy); c.scale(u, u)
        p.strokeWidth = 1.7f * weight
        if (halo) { p.strokeWidth = 3.1f * weight; p.color = 0x38000000; paint(c); p.strokeWidth = 1.7f * weight }
        p.color = color
        paint(c)
        if (level > 0.02f) {
            c.save(); c.clipRect(-2f, 24f * (1f - level.coerceAtMost(1f)), 26f, 26f)
            p.color = accent; paint(c); c.restore()
        }
        c.restore()
    }

    private fun line(c: Canvas, x1: Float, y1: Float, x2: Float, y2: Float) = c.drawLine(x1, y1, x2, y2, p)
    private fun poly(c: Canvas, vararg pts: Float) { val path = Path(); path.moveTo(pts[0], pts[1]); var i = 2; while (i < pts.size) { path.lineTo(pts[i], pts[i + 1]); i += 2 }; c.drawPath(path, p) }
    private fun rr(c: Canvas, l: Float, t: Float, r: Float, b: Float, rad: Float) = c.drawRoundRect(RectF(l, t, r, b), rad, rad, p)

    private fun paint(c: Canvas) {
        when (kind) {
            "play" -> { val path = Path(); path.moveTo(8f, 5.5f); path.lineTo(18.5f, 12f); path.lineTo(8f, 18.5f); path.close(); c.drawPath(path, p) }
            "pause" -> { p.strokeWidth = 2.6f * weight; line(c, 8.5f, 6f, 8.5f, 18f); line(c, 15.5f, 6f, 15.5f, 18f) }
            "volume", "mute" -> {
                val sp = Path(); sp.moveTo(4.5f, 10f); sp.lineTo(7.5f, 10f); sp.lineTo(11.5f, 6.5f); sp.lineTo(11.5f, 17.5f); sp.lineTo(7.5f, 14f); sp.lineTo(4.5f, 14f); sp.close(); c.drawPath(sp, p)
                if (kind == "volume") { c.drawArc(RectF(9f, 8.5f, 16f, 15.5f), -45f, 90f, false, p); c.drawArc(RectF(8f, 5.5f, 19.5f, 18.5f), -45f, 90f, false, p) }
                else { line(c, 15.5f, 10f, 19.5f, 14f); line(c, 19.5f, 10f, 15.5f, 14f) }
            }
            "expand" -> { poly(c, 5f, 9.5f, 5f, 5f, 9.5f, 5f); poly(c, 14.5f, 5f, 19f, 5f, 19f, 9.5f); poly(c, 5f, 14.5f, 5f, 19f, 9.5f, 19f); poly(c, 19f, 14.5f, 19f, 19f, 14.5f, 19f) }
            "check" -> poly(c, 5f, 12.5f, 10f, 17.5f, 19f, 7f)
            "close" -> { line(c, 6f, 6f, 18f, 18f); line(c, 18f, 6f, 6f, 18f) }
            "back" -> poly(c, 14.5f, 5f, 7.5f, 12f, 14.5f, 19f)
            "power" -> { c.drawArc(RectF(5f, 5.5f, 19f, 19.5f), -55f, 290f, false, p); line(c, 12f, 3.5f, 12f, 11f) }
            "mic" -> { rr(c, 8.5f, 2.5f, 15.5f, 14f, 3.5f); c.drawArc(RectF(5f, 6f, 19f, 19.5f), 5f, 170f, false, p); line(c, 12f, 19.5f, 12f, 22f); line(c, 8.5f, 22f, 15.5f, 22f) }
            "camera" -> { rr(c, 2.5f, 7f, 21.5f, 20f, 3.5f); c.drawCircle(12f, 13.5f, 3.6f, p); poly(c, 8f, 7f, 9.3f, 4.5f, 14.7f, 4.5f, 16f, 7f) }
            "record" -> { rr(c, 2.5f, 6.5f, 15.5f, 17.5f, 3.2f); poly(c, 15.5f, 10.5f, 21.5f, 7.5f, 21.5f, 16.5f, 15.5f, 13.5f) }
            "direction" -> { c.drawCircle(12f, 12f, 9.3f, p); poly(c, 9.6f, 9.3f, 12f, 6.8f, 14.4f, 9.3f); poly(c, 9.6f, 14.7f, 12f, 17.2f, 14.4f, 14.7f); poly(c, 9.3f, 9.6f, 6.8f, 12f, 9.3f, 14.4f); poly(c, 14.7f, 9.6f, 17.2f, 12f, 14.7f, 14.4f) }
            "move" -> { line(c, 2.5f, 12f, 21.5f, 12f); line(c, 12f, 2.5f, 12f, 21.5f); poly(c, 5.2f, 8.8f, 2.5f, 12f, 5.2f, 15.2f); poly(c, 18.8f, 8.8f, 21.5f, 12f, 18.8f, 15.2f); poly(c, 8.8f, 5.2f, 12f, 2.5f, 15.2f, 5.2f); poly(c, 8.8f, 18.8f, 12f, 21.5f, 15.2f, 18.8f) }
            "settings" -> {
                line(c, 4f, 6f, 11.8f, 6f); line(c, 17.4f, 6f, 20f, 6f); c.drawCircle(14.6f, 6f, 2.6f, p)
                line(c, 4f, 12f, 5.8f, 12f); line(c, 11.4f, 12f, 20f, 12f); c.drawCircle(8.6f, 12f, 2.6f, p)
                line(c, 4f, 18f, 11.8f, 18f); line(c, 17.4f, 18f, 20f, 18f); c.drawCircle(14.6f, 18f, 2.6f, p)
            }
            "sleep" -> { c.drawArc(RectF(3f, 3f, 21f, 21f), -52f, 284f, false, p); line(c, 12f, 1.5f, 12f, 10.5f) }
        }
    }
}
