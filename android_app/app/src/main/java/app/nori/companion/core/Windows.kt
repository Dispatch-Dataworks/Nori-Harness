package app.nori.companion.core

/**
 * The two time-boxed windows the OWNER opens on the phone: the interact (control) window and the screen-share window.
 * They are plain timestamps (epoch ms). Closed = 0 or in the past. A timestamp further in the future than the longest window can ever be is
 * treated as closed, so a clock change can never stretch a window.
 */
object Windows {
    val MINUTES_CHOICES: List<Int> = listOf(15, 60, 180)
    const val MAX_MS: Long = 3L * 3600 * 1000
    private const val SLACK_MS: Long = 60_000L

    fun isValidMinutes(m: Int) = m in MINUTES_CHOICES

    /** The end time of a window opened now for [minutes], or null when that length is not one of the allowed choices. */
    fun open(now: Long, minutes: Int): Long? = if (isValidMinutes(minutes)) now + minutes * 60_000L else null

    fun isOpen(until: Long, now: Long): Boolean = until > now && until - now <= MAX_MS + SLACK_MS

    fun remainingMs(until: Long, now: Long): Long = if (isOpen(until, now)) until - now else 0L

    /** What goes to the server in `interact_until` / `share_until`: the end time while open, otherwise 0 (never an expired time). */
    fun reportedUntil(until: Long, now: Long): Long = if (isOpen(until, now)) until else 0L

    fun label(minutes: Int): String = if (minutes < 60) "$minutes min" else "${minutes / 60} h"

    /** "12:34" style remaining time. */
    fun countdown(remainingMs: Long): String {
        val total = (remainingMs / 1000).coerceAtLeast(0)
        val h = total / 3600
        val m = (total % 3600) / 60
        val s = total % 60
        return if (h > 0) String.format(java.util.Locale.US, "%d:%02d:%02d", h, m, s) else String.format(java.util.Locale.US, "%d:%02d", m, s)
    }
}

/** A sliding one-hour budget for side-effecting commands. State is a list of timestamps, persisted as a string so a restart cannot reset it. */
class HourlyCap(stamps: List<Long> = emptyList()) {
    private val times = ArrayList<Long>(stamps)

    private fun prune(now: Long) { times.removeAll { it <= now - WINDOW_MS && it <= now } }

    /** Uses in the last hour. A timestamp from the future (the clock moved back) still counts, so it cannot be used to dodge the cap. */
    @Synchronized fun used(now: Long): Int { prune(now); return times.size }

    /** Would one more use be allowed right now? (Does not take it.) */
    @Synchronized fun wouldAllow(now: Long, limit: Int): Boolean { prune(now); return limit > 0 && times.size < limit }

    /** Takes one use if the budget allows it. A limit of 0 (or less) allows nothing. */
    @Synchronized fun tryTake(now: Long, limit: Int): Boolean {
        prune(now)
        if (limit <= 0 || times.size >= limit) return false
        times.add(now)
        return true
    }

    @Synchronized fun serialize(): String = times.joinToString(",")

    companion object {
        const val WINDOW_MS = 3_600_000L
        fun parse(s: String?): HourlyCap = HourlyCap((s ?: "").split(',').mapNotNull { it.trim().toLongOrNull() }.takeLast(500))
    }
}
