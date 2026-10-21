package app.nori.companion

import android.content.Context
import android.graphics.PixelFormat
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.view.View
import android.view.WindowManager

/**
 * Holds the screen awake while an owner-opened window needs it (the control window or the screen-share window), so Android's screen timeout does not lock the phone
 * and cut the window short. Two ways, both released the moment the last holder lets go: a one-pixel invisible window flagged keep-screen-on (needs "Display over other apps"),
 * and a screen wake lock as the fallback. It cannot wake or unlock a phone that is already off, and the power button still locks it.
 */
object KeepAwake {
    private val main = Handler(Looper.getMainLooper())
    private val holders = HashMap<String, Long>()          // reason -> held until (ms)
    private var view: View? = null
    private var lock: PowerManager.WakeLock? = null

    fun hold(c: Context, reason: String, untilMs: Long) {
        if (!Prefs.keepAwake(c)) return
        val app = c.applicationContext
        main.post { holders[reason] = untilMs; apply(app) }
    }

    fun release(c: Context, reason: String) {
        val app = c.applicationContext
        main.post { holders.remove(reason); apply(app) }
    }

    private fun apply(c: Context) {
        val now = System.currentTimeMillis()
        holders.entries.removeAll { it.value <= now }
        if (holders.isEmpty()) {
            try { view?.let { (c.getSystemService(Context.WINDOW_SERVICE) as WindowManager).removeView(it) } } catch (e: Exception) { /* already gone */ }
            view = null
            try { if (lock?.isHeld == true) lock?.release() } catch (e: Exception) { /* already released */ }
            lock = null
            return
        }
        val until = holders.values.max()
        if (view == null && Settings.canDrawOverlays(c)) {
            try {
                val v = View(c)
                val lp = WindowManager.LayoutParams(1, 1, WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                    WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON, PixelFormat.TRANSLUCENT)
                (c.getSystemService(Context.WINDOW_SERVICE) as WindowManager).addView(v, lp)
                view = v
            } catch (e: Exception) { SentLog.add(c, "could not keep the screen on with a window: ${e.javaClass.simpleName}") }
        }
        try {
            lock?.let { if (it.isHeld) it.release() }
            @Suppress("DEPRECATION")
            val wl = (c.getSystemService(Context.POWER_SERVICE) as PowerManager).newWakeLock(PowerManager.SCREEN_BRIGHT_WAKE_LOCK, "nori:awake")
            wl.setReferenceCounted(false)
            wl.acquire((until - now).coerceIn(1_000L, 3L * 3600 * 1000 + 60_000L) + 60_000L)
            lock = wl
        } catch (e: Exception) { SentLog.add(c, "could not hold a screen wake lock: ${e.javaClass.simpleName}") }
    }
}
