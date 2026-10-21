package app.nori.companion.core

/** The notification channels. Importance numbers are Android's NotificationManager.IMPORTANCE_* values (LOW=2, DEFAULT=3, HIGH=4), kept here so the mapping is testable. */
object Channels {
    const val IMPORTANCE_LOW = 2
    const val IMPORTANCE_DEFAULT = 3
    const val IMPORTANCE_HIGH = 4

    const val CHAT = "nori_chat"
    const val REMINDER = "nori_reminder"
    const val APPROVAL = "nori_approval"
    const val JOB = "nori_job"
    const val PEER = "nori_peer"
    const val SYNC = "nori_sync"
    const val UPDATES = "nori_updates"
    const val WINDOWS = "nori_windows"

    class Spec(val id: String, val name: String, val description: String, val importance: Int)

    val ALL: List<Spec> = listOf(
        Spec(CHAT, "Nori's replies", "Replies and messages from Nori when the app is not open", IMPORTANCE_HIGH),
        Spec(REMINDER, "Reminders", "Reminders you asked Nori for", IMPORTANCE_HIGH),
        Spec(APPROVAL, "Approvals", "When Nori needs your yes or no before doing something", IMPORTANCE_HIGH),
        Spec(JOB, "Finished jobs", "When a background job Nori started is done", IMPORTANCE_DEFAULT),
        Spec(PEER, "Other assistants", "Messages from other household assistants", IMPORTANCE_LOW),
        Spec(SYNC, "Connection", "The small ongoing notice that Nori is connected, with a pause button", IMPORTANCE_LOW),
        Spec(UPDATES, "App updates", "Tells you when a newer Nori app is ready to install", IMPORTANCE_DEFAULT),
        Spec(WINDOWS, "Control and screen windows", "Counts down the time you opened for Nori to act on, or look at, this phone", IMPORTANCE_LOW),
    )

    /** Category drives the channel: chat/ping -> replies, reminder, approval, job, peer. An unknown category is treated as a job-level notice, never as an interrupting one. */
    fun forCategory(category: String): String = when (category.lowercase()) {
        "chat", "ping" -> CHAT
        "reminder" -> REMINDER
        "approval" -> APPROVAL
        "job" -> JOB
        "peer" -> PEER
        else -> JOB
    }

    fun importanceOf(channelId: String): Int = ALL.firstOrNull { it.id == channelId }?.importance ?: IMPORTANCE_DEFAULT

    fun titleFor(m: Message): String = m.title.ifBlank { "Nori" }

    /** Stable notification id for a server message id (fits an Int, never collides with the app's fixed ids below 100). */
    fun notificationId(messageId: Long): Int = (100 + Math.floorMod(messageId, 1_000_000_000L)).toInt()
}

/** A size-limited, newest-last text log (the "everything this app sent" list). The Android wrapper only reads and writes the lines. */
class RingLog(lines: List<String> = emptyList(), private val max: Int = 120) {
    private val items = ArrayList<String>(lines.takeLast(max))

    @Synchronized fun add(line: String) {
        items.add(line.replace('\n', ' ').replace('\r', ' ').take(400))
        while (items.size > max) items.removeAt(0)
    }

    @Synchronized fun all(): List<String> = items.toList()
    @Synchronized fun recent(n: Int): List<String> = items.takeLast(n).reversed()
    @Synchronized fun clear() { items.clear() }
    @Synchronized fun serialize(): String = items.joinToString("\n")

    companion object {
        const val MAX = 120
        fun parse(text: String?): RingLog = RingLog((text ?: "").split('\n').filter { it.isNotEmpty() }, MAX)
    }
}
