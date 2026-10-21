package app.nori.companion.core

/** What happened when the app tried to turn one message into a notification. */
enum class ShowOutcome {
    /** Shown. */
    SHOWN,
    /** Deliberately not shown (quiet hours, notifications switched off by the owner): counts as handled. */
    SKIPPED,
    /** Could not be shown (notification permission missing, system error): NOT handled, so it is not acknowledged and the server sends it again. */
    FAILED,
}

/**
 * The notification cursor. The server resends everything above `ack`, so a message must only be acknowledged once it has really been dealt with.
 * Messages are processed oldest first and processing STOPS at the first failure, so the ack can never jump over an unshown message.
 * Already-seen ids (a redelivery after a lost response, or the same id twice in one poll) are not shown again.
 */
class MessageInbox(ackedUpTo: Long = 0L, seen: Collection<Long> = emptyList()) {
    private var ack: Long = ackedUpTo
    private val seen = LinkedHashSet<Long>(seen)

    @Synchronized fun ackValue(): Long = ack
    @Synchronized fun hasSeen(id: Long) = id in seen

    /** Walks [messages] (any order, duplicates allowed) and returns how many were newly shown. */
    @Synchronized fun process(messages: List<Message>, show: (Message) -> ShowOutcome): Int {
        var shown = 0
        val ordered = messages.sortedBy { it.id }
        var lastId = Long.MIN_VALUE
        for (m in ordered) {
            if (m.id == lastId) continue
            lastId = m.id
            if (m.id in seen) { ack = maxOf(ack, m.id); continue }
            when (show(m)) {
                ShowOutcome.SHOWN -> { remember(m.id); ack = maxOf(ack, m.id); shown++ }
                ShowOutcome.SKIPPED -> { remember(m.id); ack = maxOf(ack, m.id) }
                ShowOutcome.FAILED -> return shown
            }
        }
        return shown
    }

    private fun remember(id: Long) {
        seen.add(id)
        while (seen.size > MAX_SEEN) seen.remove(seen.first())
    }

    @Synchronized fun serializeSeen(): String = seen.joinToString(",")

    companion object {
        const val MAX_SEEN = 300
        fun parseSeen(s: String?): List<Long> = (s ?: "").split(',').mapNotNull { it.trim().toLongOrNull() }
    }
}

/** Should a message become a notification, given the settings? (The server filters too; this is the phone's own half.) */
object NotifyPolicy {
    /** During the owner's quiet hours only reminders and approvals get through. */
    fun allowedWhenQuiet(category: String): Boolean = category == "reminder" || category == "approval"

    fun decide(m: Message, serverNotifications: Boolean, ownerNotifications: Boolean, quiet: Boolean, paused: Boolean): Boolean {
        if (paused || !serverNotifications || !ownerNotifications) return false
        if (quiet && !allowedWhenQuiet(m.category)) return false
        return true
    }
}

/**
 * Commands can arrive more than once (the server redelivers an un-acked command after 4 s, at most twice). Each (id, approved) pair runs once:
 * the approved re-send of a command that earlier answered needs_approval is a different execution and is allowed, while a plain repeat is not.
 */
class CommandTracker(private val capacity: Int = 100) {
    enum class Verdict { NEW, IN_FLIGHT, DONE }

    private val state = LinkedHashMap<String, Boolean>() // key -> finished

    private fun key(c: Command) = "${c.id}:${if (c.approved) 1 else 0}"

    @Synchronized fun begin(c: Command): Verdict {
        val k = key(c)
        val s = state[k]
        if (s == null) {
            state[k] = false
            while (state.size > capacity) state.remove(state.keys.first())
            return Verdict.NEW
        }
        return if (s) Verdict.DONE else Verdict.IN_FLIGHT
    }

    @Synchronized fun finish(c: Command) { state[key(c)] = true }
}

/** How the poll loop reacts to an HTTP status. */
enum class PollAction { OK, RE_PAIR, BACK_OFF_LONG, SLOW_DOWN, RETRY }

object PollPolicy {
    const val SERVER_HOLD_MS = 25_000L
    const val CLIENT_TIMEOUT_MS = 40_000
    const val BACKOFF_START_MS = 2_000L
    const val BACKOFF_MAX_MS = 60_000L
    const val FORBIDDEN_WAIT_MS = 60_000L
    const val SLOW_DOWN_WAIT_MS = 30_000L
    const val MIN_LOOP_MS = 800L

    fun classify(code: Int): PollAction = when {
        code in 200..299 -> PollAction.OK
        code == 401 -> PollAction.RE_PAIR
        code == 403 -> PollAction.BACK_OFF_LONG
        code == 429 -> PollAction.SLOW_DOWN
        else -> PollAction.RETRY
    }
}

/** 2 s, doubling to 60 s; one success starts it over. */
class Backoff {
    private var next = PollPolicy.BACKOFF_START_MS
    fun failure(): Long { val d = next; next = minOf(next * 2, PollPolicy.BACKOFF_MAX_MS); return d }
    fun success() { next = PollPolicy.BACKOFF_START_MS }

    /** How long to wait after a poll outcome; also advances / resets the backoff. 0 = poll again straight away. */
    fun wait(action: PollAction): Long = when (action) {
        PollAction.OK -> { success(); 0L }
        PollAction.BACK_OFF_LONG -> PollPolicy.FORBIDDEN_WAIT_MS
        PollAction.SLOW_DOWN -> maxOf(PollPolicy.SLOW_DOWN_WAIT_MS, failure())
        PollAction.RETRY -> failure()
        PollAction.RE_PAIR -> PollPolicy.FORBIDDEN_WAIT_MS
    }
}
