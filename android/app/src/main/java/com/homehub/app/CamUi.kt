package com.homehub.app

import android.animation.ValueAnimator
import android.app.Activity
import android.app.Dialog
import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RectF
import android.graphics.Typeface
import android.graphics.drawable.ColorDrawable
import android.graphics.drawable.GradientDrawable
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

/**
 * The camera page's pieces in HomeHub's own look (the web page's app.css): the sky gradient, white cards, the floating sheet
 * (8 px from the edges, 22 px corners, centred title), the round switch and the chips. Native, but it should not be told apart
 * from the rest of the app.
 */
object Hh {
    val ink = Color.parseColor("#23232A")
    val sub = Color.parseColor("#7D7A86")
    val line = 0x14463C50
    val card2 = 0x17786E7D
    val blue = Color.parseColor("#2F80ED")
    val off = Color.parseColor("#A9A6B0")
    val sky get() = GradientDrawable(GradientDrawable.Orientation.TOP_BOTTOM,
        intArrayOf(Color.parseColor("#7F86B5"), Color.parseColor("#A597B8"), Color.parseColor("#CFA9B1"), Color.parseColor("#E8CFC6"), Color.parseColor("#F1E4DD")))

    fun dp(c: Context, v: Int) = (v * c.resources.displayMetrics.density).toInt()
    fun card(c: Context, color: Int = 0x9EFFFFFF.toInt(), r: Int = 22) = GradientDrawable().apply { setColor(color); cornerRadius = dp(c, r).toFloat() }

    fun text(c: Context, t: String, size: Float, color: Int = ink, bold: Boolean = false) = TextView(c).apply {
        text = t; textSize = size; setTextColor(color); if (bold) setTypeface(null, Typeface.BOLD)
    }
}

/** HomeHub's switch: a grey pill that turns blue, the white knob slides across. */
class HhSwitch(ctx: Context, on: Boolean, private val onChange: (Boolean) -> Unit) : View(ctx) {
    var checked = on; private set
    private var pos = if (on) 1f else 0f
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)

    init {
        setOnClickListener { set(!checked); onChange(checked) }
    }

    fun set(v: Boolean) {
        if (v == checked) return
        checked = v
        ValueAnimator.ofFloat(pos, if (v) 1f else 0f).apply { duration = 180; addUpdateListener { pos = it.animatedValue as Float; invalidate() } }.start()
    }

    override fun onMeasure(w: Int, h: Int) = setMeasuredDimension(Hh.dp(context, 50), Hh.dp(context, 30))

    override fun onDraw(c: Canvas) {
        val w = width.toFloat(); val h = height.toFloat(); val r = h / 2
        paint.color = blend(Hh.off, Hh.blue, pos)
        c.drawRoundRect(RectF(0f, 0f, w, h), r, r, paint)
        paint.color = Color.WHITE
        val k = h * 0.4f
        c.drawCircle(r + (w - 2 * r) * pos, r, k, paint)
    }

    private fun blend(a: Int, b: Int, t: Float): Int {
        fun ch(s: Int) = ((a shr s and 0xFF) + (((b shr s and 0xFF) - (a shr s and 0xFF)) * t)).toInt() shl s
        return (0xFF shl 24) or ch(16) or ch(8) or ch(0)
    }
}

/** HomeHub's sheet: a white card floating 8 dp above the bottom, a dimmed page behind it, sliding up. */
class HhSheet(private val a: Activity, title: String) {
    val dialog = Dialog(a)
    val body: LinearLayout
    private fun dp(v: Int) = Hh.dp(a, v)

    init {
        val card = LinearLayout(a).apply {
            orientation = LinearLayout.VERTICAL; background = Hh.card(a, Color.WHITE, 22); setPadding(dp(18), dp(20), dp(18), dp(16))
        }
        card.addView(Hh.text(a, title, 17f, bold = true).apply { gravity = Gravity.CENTER }, LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = dp(10) })
        body = LinearLayout(a).apply { orientation = LinearLayout.VERTICAL }
        val sv = ScrollView(a).apply { isVerticalScrollBarEnabled = false; overScrollMode = View.OVER_SCROLL_NEVER; addView(body) }
        card.addView(sv, LinearLayout.LayoutParams(-1, -2))
        dialog.setContentView(card)
        dialog.window?.apply {
            setBackgroundDrawable(ColorDrawable(Color.TRANSPARENT))
            setGravity(Gravity.BOTTOM)
            setLayout(a.resources.displayMetrics.widthPixels - dp(16), ViewGroup.LayoutParams.WRAP_CONTENT)
            attributes = attributes.apply { y = dp(8); dimAmount = 0.38f }
            addFlags(WindowManager.LayoutParams.FLAG_DIM_BEHIND)
            setWindowAnimations(android.R.style.Animation_InputMethod)
        }
        // never taller than 70 % of the screen, like the web sheet
        sv.addOnLayoutChangeListener { v, _, t, _, b, _, _, _, _ ->
            val max = (a.resources.displayMetrics.heightPixels * 0.7).toInt()
            if (b - t > max) { v.layoutParams = v.layoutParams.apply { height = max } }
        }
    }

    fun show() = dialog.show()
    fun dismiss() = dialog.dismiss()

    /** A grey group label, like .sheet-group */
    fun group(t: String) = body.addView(Hh.text(a, t, 12.5f, Hh.sub).apply { setPadding(dp(4), dp(14), 0, dp(6)) })

    /** A rounded light panel that holds rows separated by thin lines. */
    fun panel(): LinearLayout = LinearLayout(a).apply {
        orientation = LinearLayout.VERTICAL; background = Hh.card(a, Hh.card2, 16); setPadding(dp(14), 0, dp(14), 0)
        body.addView(this, LinearLayout.LayoutParams(-1, -2))
    }

    fun row(panel: LinearLayout, label: String, note: String? = null, right: View?, onClick: (() -> Unit)? = null): LinearLayout {
        if (panel.childCount > 0) panel.addView(View(a).apply { setBackgroundColor(Hh.line) }, LinearLayout.LayoutParams(-1, 1))
        val row = LinearLayout(a).apply {
            orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL; minimumHeight = dp(52); setPadding(0, dp(8), 0, dp(8))
            if (onClick != null) setOnClickListener { onClick() }
        }
        val texts = LinearLayout(a).apply { orientation = LinearLayout.VERTICAL }
        texts.addView(Hh.text(a, label, 15f))
        if (note != null) texts.addView(Hh.text(a, note, 12f, Hh.sub).apply { setPadding(0, dp(2), 0, 0) })
        row.addView(texts, LinearLayout.LayoutParams(0, -2, 1f))
        if (right != null) row.addView(right)
        panel.addView(row)
        return row
    }

    /** A row of chips (one chosen), like .chips in the web sheets. */
    fun chips(panel: LinearLayout, label: String, opts: List<Pair<String, Any>>, cur: Any?, pick: (Any) -> Unit) {
        if (panel.childCount > 0) panel.addView(View(a).apply { setBackgroundColor(Hh.line) }, LinearLayout.LayoutParams(-1, 1))
        panel.addView(Hh.text(a, label, 15f).apply { setPadding(0, dp(14), 0, dp(10)) })
        val line = LinearLayout(a).apply { orientation = LinearLayout.HORIZONTAL; setPadding(0, 0, 0, dp(14)) }
        val cells = ArrayList<TextView>()
        fun paint(c: TextView, on: Boolean) {
            c.background = Hh.card(a, if (on) Hh.blue else 0x14000000, 14)
            c.setTextColor(if (on) Color.WHITE else Hh.ink); c.setTypeface(null, if (on) Typeface.BOLD else Typeface.NORMAL)
        }
        opts.forEachIndexed { i, (name, v) ->
            val c = Hh.text(a, name, 14f).apply { gravity = Gravity.CENTER; setPadding(dp(4), dp(9), dp(4), dp(9)) }
            c.setOnClickListener { pick(v); cells.forEachIndexed { j, cc -> paint(cc, j == i) } }
            paint(c, v.toString() == cur.toString())
            cells.add(c)
            line.addView(c, LinearLayout.LayoutParams(0, -2, 1f).apply { if (i < opts.size - 1) rightMargin = dp(8) })
        }
        panel.addView(line)
    }
}
