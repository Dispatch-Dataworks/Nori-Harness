package app.nori.companion

import android.content.Context
import app.nori.companion.core.RingLog
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * "Everything this app has sent" (the last 120 entries), so the owner can always see what left the phone and what the phone did for Nori.
 * Lines describe WHAT happened (never the content of a screen, a message, or a coordinate).
 */
object SentLog {
    private var ring: RingLog? = null
    private fun fmt() = SimpleDateFormat("MMM d HH:mm:ss", Locale.US)

    private fun file(c: Context) = File(c.applicationContext.filesDir, "sent.log")

    private fun load(c: Context): RingLog {
        ring?.let { return it }
        val r = try { RingLog.parse(file(c).takeIf { it.exists() }?.readText()) } catch (e: Exception) { RingLog() }
        ring = r
        return r
    }

    @Synchronized
    fun add(c: Context, line: String) {
        val r = load(c)
        r.add("${fmt().format(Date())}  $line")
        try { file(c).writeText(r.serialize()) } catch (e: Exception) { /* the log must never break the app */ }
    }

    @Synchronized
    fun recent(c: Context, n: Int = 40): List<String> = load(c).recent(n)

    @Synchronized
    fun clear(c: Context) {
        load(c).clear()
        try { file(c).delete() } catch (e: Exception) { /* nothing */ }
    }
}
