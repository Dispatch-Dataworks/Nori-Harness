package app.nori.companion

import android.app.Activity
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.Looper
import android.util.Base64
import android.view.WindowManager
import android.widget.Toast
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import androidx.core.content.IntentCompat
import app.nori.companion.core.Windows
import java.io.ByteArrayOutputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/** One capture: a JPEG (base64) or an error text. */
class Shot(val jpegB64: String?, val error: String?)

/**
 * The screen-share window. The OWNER starts it here on the phone for 15 min, 1 h or 3 h; Android asks for its own consent each time and shows its own capture
 * indicator while it runs (this app never hides that). While it is open the server may send a `screenshot` command; each one is answered with a single picture
 * taken at that moment. Frames arriving in between are thrown away unread, nothing is ever written to storage, and when the time is up or the owner stops it the
 * projection is released. It cannot tap, type or open anything.
 */
class ScreenShareService : Service() {
    private val handler = Handler(Looper.getMainLooper())
    private var projection: MediaProjection? = null
    private var reader: ImageReader? = null
    private var display: VirtualDisplay? = null
    private var worker: HandlerThread? = null
    private val pending = AtomicReference<Pending?>(null)
    private var endsAt = 0L
    private val stopRunnable = Runnable { endSession("the time was up") }

    private class Pending(val latch: CountDownLatch = CountDownLatch(1), @Volatile var bytes: ByteArray? = null, @Volatile var error: String? = null)

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> start(intent)
            ACTION_STOP -> endSession("you stopped it")
            else -> stopSelf()
        }
        return START_NOT_STICKY
    }

    private fun start(intent: Intent) {
        val minutes = intent.getIntExtra("minutes", 15)
        val resultCode = intent.getIntExtra("resultCode", 0)
        val data = IntentCompat.getParcelableExtra(intent, "data", Intent::class.java)
        val until = Windows.open(System.currentTimeMillis(), minutes)
        if (data == null || resultCode == 0 || until == null || WindowControl.shareBlocker(this) != null) { stopSelf(); return }
        endsAt = until
        try {
            ServiceCompat.startForeground(this, Notifier.ID_SHARE, Notifier.shareNotification(this, until), ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)
            val mpm = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
            val p = mpm.getMediaProjection(resultCode, data) ?: throw IllegalStateException("Android returned no projection")
            projection = p
            p.registerCallback(object : MediaProjection.Callback() { override fun onStop() { handler.post { endSession("Android ended it") } } }, handler)
            val bounds = (getSystemService(Context.WINDOW_SERVICE) as WindowManager).currentWindowMetrics.bounds
            val long = maxOf(bounds.width(), bounds.height()).toFloat()
            val scale = if (long > 1600f) 1600f / long else 1f
            val w = (bounds.width() * scale).toInt().coerceAtLeast(2) and 1.inv()
            val h = (bounds.height() * scale).toInt().coerceAtLeast(2) and 1.inv()
            val t = HandlerThread("nori-capture").also { it.start() }
            worker = t
            val r = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 2)
            r.setOnImageAvailableListener({ ir -> onFrame(ir) }, Handler(t.looper))
            reader = r
            display = p.createVirtualDisplay("nori-share", w, h, resources.displayMetrics.densityDpi, DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, r.surface, null, handler)
        } catch (e: Exception) {
            SentLog.add(this, "screen sharing could not start: ${e.javaClass.simpleName}: ${(e.message ?: "").take(100)}")
            endSession("it could not start")
            return
        }
        current = this
        Prefs.setShareUntil(this, endsAt)
        KeepAwake.hold(this, "share", endsAt)
        handler.postDelayed(stopRunnable, (endsAt - System.currentTimeMillis()).coerceAtLeast(1_000L))
        SentLog.add(this, "screen-share window OPENED by you for ${Windows.label(minutes)}")
        SyncService.pollNow(this)
        TileRefresh.all(this)
    }

    /** Every frame is read and dropped, unless a screenshot was just asked for: then this one frame becomes the answer. */
    private fun onFrame(ir: ImageReader) {
        val img = try { ir.acquireLatestImage() } catch (e: Exception) { null } ?: return
        try {
            val want = pending.getAndSet(null) ?: return
            try {
                val plane = img.planes[0]
                val pad = plane.rowStride - plane.pixelStride * img.width
                val full = Bitmap.createBitmap(img.width + pad / plane.pixelStride, img.height, Bitmap.Config.ARGB_8888)
                plane.buffer.rewind()
                full.copyPixelsFromBuffer(plane.buffer)
                val bmp = if (pad == 0) full else Bitmap.createBitmap(full, 0, 0, img.width, img.height).also { full.recycle() }
                var quality = 70
                var bytes: ByteArray
                do {
                    val out = ByteArrayOutputStream()
                    bmp.compress(Bitmap.CompressFormat.JPEG, quality, out)
                    bytes = out.toByteArray()
                    quality -= 15
                } while (bytes.size > MAX_JPEG_BYTES && quality >= 25)
                bmp.recycle()
                if (bytes.size > MAX_JPEG_BYTES) want.error = "the screenshot is too large to send" else want.bytes = bytes
            } catch (e: Exception) {
                want.error = "capture failed: ${e.javaClass.simpleName}"
            }
            want.latch.countDown()
        } finally { try { img.close() } catch (e: Exception) { /* closed */ } }
    }

    private fun endSession(why: String) {
        if (current === this) current = null
        handler.removeCallbacks(stopRunnable)
        KeepAwake.release(this, "share")
        val was = Prefs.shareUntil(this) > 0L
        Prefs.setShareUntil(this, 0L)
        pending.getAndSet(null)?.let { it.error = "the screen-share window ended"; it.latch.countDown() }
        try { display?.release() } catch (e: Exception) { /* gone */ }
        try { reader?.close() } catch (e: Exception) { /* gone */ }
        try { projection?.stop() } catch (e: Exception) { /* gone */ }
        try { worker?.quitSafely() } catch (e: Exception) { /* gone */ }
        display = null; reader = null; projection = null; worker = null
        if (was) SentLog.add(this, "screen-share window CLOSED ($why)")
        try { stopForeground(STOP_FOREGROUND_REMOVE) } catch (e: Exception) { /* not foreground */ }
        stopSelf()
        SyncService.pollNow(this)
        TileRefresh.all(this)
    }

    override fun onDestroy() {
        KeepAwake.release(this, "share")
        if (current === this) current = null
        super.onDestroy()
    }

    /** One picture, now. Only while the window is open. */
    fun grab(): Shot {
        if (!Windows.isOpen(endsAt, System.currentTimeMillis())) return Shot(null, "the screen-share window has closed")
        val p = Pending()
        pending.set(p)
        // a still screen produces no new frames, so re-attach the surface to make the system draw one now
        handler.post { try { val s = reader?.surface; display?.surface = null; display?.surface = s } catch (e: Exception) { /* the frame callback will still catch the next change */ } }
        if (!p.latch.await(4, TimeUnit.SECONDS)) {
            pending.compareAndSet(p, null)
            return Shot(null, "no frame from the screen yet (the screen may be off)")
        }
        val bytes = p.bytes ?: return Shot(null, p.error ?: "capture failed")
        return Shot(Base64.encodeToString(bytes, Base64.NO_WRAP), null)
    }

    companion object {
        const val ACTION_START = "app.nori.companion.SHARE_START"
        const val ACTION_STOP = "app.nori.companion.SHARE_STOP"
        const val MAX_JPEG_BYTES = 1_500_000

        @Volatile
        var current: ScreenShareService? = null

        fun start(c: Context, resultCode: Int, data: Intent, minutes: Int) {
            val i = Intent(c, ScreenShareService::class.java).setAction(ACTION_START).putExtra("resultCode", resultCode).putExtra("data", data).putExtra("minutes", minutes)
            ContextCompat.startForegroundService(c, i)
        }

        fun stop(c: Context) {
            val s = current
            if (s != null) s.handler.post { s.endSession("you stopped it") }
            else { Prefs.setShareUntil(c, 0L) }
        }

        fun active(): Boolean = current != null
    }
}

/** Asks Android for its screen-capture consent (always the owner's tap, on the phone), then starts the share window. Opened by the tile and the settings screen. */
class ScreenShareRequestActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val problem = WindowControl.shareBlocker(this)
        if (problem != null) { Toast.makeText(this, problem, Toast.LENGTH_LONG).show(); finish(); return }
        val minutes = intent.getIntExtra(EXTRA_MINUTES, 15)
        if (!Windows.isValidMinutes(minutes)) { finish(); return }
        val mpm = getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        @Suppress("DEPRECATION")
        startActivityForResult(mpm.createScreenCaptureIntent(), 91)
    }

    @Deprecated("the classic result API keeps this one-shot screen simple")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == 91 && resultCode == RESULT_OK && data != null) ScreenShareService.start(this, resultCode, data, intent.getIntExtra(EXTRA_MINUTES, 15))
        else Toast.makeText(this, "Screen sharing was not allowed", Toast.LENGTH_SHORT).show()
        finish()
    }

    companion object { const val EXTRA_MINUTES = "minutes" }
}
