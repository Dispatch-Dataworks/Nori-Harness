package app.nori.companion.core

import org.json.JSONArray
import org.json.JSONObject

/**
 * How much of the phone's notifications one connection (one Nori server) may see. Off is the default everywhere and for everyone. "apps" shares only the notifications of the packages the
 * server listed (`capture_apps`); "all" shares every notification the built-in private-app list and the server's hidden list do not remove. Anything unknown is read as OFF.
 */
enum class CaptureLevel(val wire: String) {
    OFF("off"), APPS("apps"), ALL("all");

    companion object {
        fun parse(s: String?): CaptureLevel = when (s?.trim()?.lowercase()) { "apps" -> APPS; "all" -> ALL; else -> OFF }
    }
}

/** One notification as the system handed it over (nothing cleaned yet). Plain values, so the rules below are JVM-testable. */
class RawNotification(
    val pkg: String,
    val app: String,
    val title: String,
    val text: String,
    val sub: String,
    val category: String,
    val key: String,
    val ts: Long,
    val ongoing: Boolean = false,
    val groupSummary: Boolean = false,
    /** Android's own VISIBILITY_SECRET: the app asked that this never show on a lock screen. It is never read. */
    val secret: Boolean = false,
)

/** What leaves the phone for one notification: app, title and text, already cleaned and capped. `ts` is epoch milliseconds. */
data class Notif(val pkg: String, val app: String, val title: String, val text: String, val sub: String, val category: String, val key: String, val ts: Long) {
    fun toJson(): JSONObject = JSONObject().put("pkg", pkg).put("app", app).put("title", title).put("text", text).put("sub", sub).put("category", category).put("key", key).put("ts", ts)

    companion object {
        fun fromJson(o: JSONObject): Notif? {
            val pkg = o.optString("pkg", "")
            if (pkg.isEmpty()) return null
            return Notif(pkg, o.optString("app", pkg), o.optString("title", ""), o.optString("text", ""), o.optString("sub", ""), o.optString("category", ""), o.optString("key", ""), o.optLong("ts", 0L))
        }
    }
}

/** Everything the phone must know about one connection to decide what it may capture for it. The levels are the two keys: the server's and the phone's own. */
class SlotCapture(
    val slot: Int,
    /** Paired, not told to re-pair, and the phone not paused. */
    val active: Boolean,
    val server: CaptureLevel,
    val phone: CaptureLevel,
    val serverApps: List<String>,
    val hidden: List<String>,
    val hideCodes: Boolean = true,
    /** Texts (SMS) are a separate, deeper option: the server's half, the phone's own switch for this connection, and whether the server allows short-code senders. */
    val sms: Boolean = false,
    val smsPhone: Boolean = false,
    val smsShort: Boolean = false,
    /** The default SMS app's package while text capture is on for this connection, so the same text is not shared twice (once as a text, once as that app's notification). */
    val smsDedupePkg: String = "",
) {
    val effective: CaptureLevel get() = Capture.effective(server, phone)
}

/** Hide one-time codes ON THE PHONE before a notification is queued (the server hides them again). Errs toward hiding; ordinary numbers (times, prices, percentages) survive. */
object Redact {
    private val WORDS = Regex("(code|otp|passcode|verification|verify|\\bpin\\b|one.?time|2fa|two.?factor|security|sign.?in|log.?in|password|authenticat|confirm)", RegexOption.IGNORE_CASE)
    // 4-8 digits not part of a time, price, date or decimal (a separator counts only when digits sit on both sides, so a code before a full stop is still hidden) (\x24 is the dollar sign), and not followed by a unit.
    private val RUN = Regex("(?<![\\d\\x24\u20AC\u00A3#]|\\d[:.,/-])\\d{4,8}(?!\\d|[:.,/-]\\d|%|\\s?(?:am|pm|min|mins|minutes|hours?|km|mi|kg|lbs?|mph)\\b)", RegexOption.IGNORE_CASE)
    private val SPLIT = Regex("(?<![\\d\\x24\u20AC\u00A3#]|\\d[:.,/-])\\d{3}[ -]\\d{3,4}(?!\\d|[:.,/-]\\d|%)")
    private const val WINDOW = 40
    private const val MASK = "••••"

    private fun near(text: String, r: IntRange): Boolean = WORDS.containsMatchIn(text.substring(maxOf(0, r.first - WINDOW), minOf(text.length, r.last + 1 + WINDOW)))

    fun codes(text: String): String {
        if (text.isEmpty() || !WORDS.containsMatchIn(text)) return text
        val once = RUN.replace(text) { m -> if (near(text, m.range)) MASK else m.value }
        return SPLIT.replace(once) { m -> if (near(once, m.range)) MASK else m.value }
    }
}

object Capture {
    const val MAX_PER_CALL = 60
    const val MAX_QUEUED = 200
    const val MAX_AGE_MS = 6L * 60 * 60 * 1000
    const val TITLE_MAX = 160
    const val TEXT_MAX = 600
    const val SUB_MAX = 120
    const val MAX_RECENT_APPS = 40

    /** Android Notification.CATEGORY_* values that are status noise, not something a person or a device said. */
    val NOISE_CATEGORIES: Set<String> = setOf("progress", "transport", "alarm", "service", "status", "sys", "navigation", "stopwatch")

    /** Off < apps < all. A connection shares only what BOTH its server and the phone's own setting for it allow. */
    fun effective(server: CaptureLevel, phone: CaptureLevel): CaptureLevel = if (server.ordinal <= phone.ordinal) server else phone

    /** The `caps.capture` flag one connection reports: the owner turned sharing on for it AND Android's notification access is granted. */
    fun capsFlag(phone: CaptureLevel, accessGranted: Boolean): Boolean = accessGranted && phone != CaptureLevel.OFF

    /** True when at least one connection could receive something. When false the listener returns at once and nothing is read, queued or sent. */
    fun anyWanted(slots: List<SlotCapture>): Boolean = slots.any { it.active && it.effective != CaptureLevel.OFF }

    // ── incoming texts (SMS): a separate switch per connection, independent of the notification level ──
    const val SMS_PKG = "sms"
    const val SMS_APP = "Text message"

    /** Texts are shared with a connection only when the server allows it AND this phone's switch for it is on AND RECEIVE_SMS is granted AND the connection is usable and the phone not paused. */
    fun smsEffective(s: SlotCapture, granted: Boolean): Boolean = s.active && s.sms && s.smsPhone && granted

    /** The `caps.sms` flag one connection reports: the owner turned text sharing on for it AND RECEIVE_SMS is granted. */
    fun smsCapsFlag(phoneSwitch: Boolean, granted: Boolean): Boolean = granted && phoneSwitch

    fun anyWantedSms(slots: List<SlotCapture>, granted: Boolean): Boolean = slots.any { smsEffective(it, granted) }

    /** A short-code sender (all digits, six or fewer) or an alphanumeric sender id ("MYBANK"): businesses, one-time-code services, marketing. Ordinary phone numbers are neither. */
    fun isShortSender(address: String): Boolean {
        val a = address.trim()
        if (a.isEmpty()) return true
        if (a.any { it.isLetter() }) return true
        val digits = a.filter { it.isDigit() }
        return digits.length <= 6
    }

    /** What this connection receives for one incoming text, or null (switch off, short-code sender not allowed, empty). The same hide-codes rule as notifications applies; `hidden_apps` does not (a text is not an app). */
    fun smsFor(address: String, body: String, ts: Long, s: SlotCapture, granted: Boolean): Notif? {
        if (!smsEffective(s, granted)) return null
        if (isShortSender(address) && !s.smsShort) return null
        var title = plain(address, TITLE_MAX)
        var text = plain(body, TEXT_MAX)
        if (s.hideCodes) { title = Redact.codes(title); text = Redact.codes(text) }
        if (text.isEmpty()) return null
        return Notif(SMS_PKG, SMS_APP, title, text, "", "sms", "", ts)
    }

    /** True when the owner chose sharing for at least one active connection (used only for the display-only list of apps that notified). */
    fun anyPhoneLevelOn(slots: List<SlotCapture>): Boolean = slots.any { it.active && it.phone != CaptureLevel.OFF }

    /** Server-listed packages: exact package names only. A wildcard or "*" can never turn "chosen apps" into "everything". */
    fun cleanAppList(list: Collection<String>): List<String> =
        list.map { AppGuard.normalize(it) }.filter { AppGuard.isValidPackageName(it) }.distinct().take(500)

    fun mayShare(level: CaptureLevel, pkg: String, serverApps: Collection<String>): Boolean = when (level) {
        CaptureLevel.OFF -> false
        CaptureLevel.ALL -> true
        CaptureLevel.APPS -> serverApps.any { AppGuard.normalize(it) == AppGuard.normalize(pkg) }
    }

    /** Why this notification is never captured for any connection, or null. */
    fun skipReason(n: RawNotification): String? {
        if (n.pkg.isBlank()) return "no package"
        if (AppGuard.isBuiltInDenied(n.pkg)) return "private app"
        if (n.ongoing) return "ongoing"
        if (n.groupSummary) return "group summary"
        if (n.secret) return "marked secret"
        if (n.category.lowercase() in NOISE_CATEGORIES) return "status noise"
        if (n.title.isBlank() && n.text.isBlank()) return "empty"
        return null
    }

    private fun plain(s: String, max: Int): String = s.replace(Regex("[\\u0000-\\u001f\\u007f]+"), " ").replace(Regex(" {2,}"), " ").trim().take(max)

    /** What this connection receives for [n], or null when it receives nothing for it (level, chosen apps, hidden list, the global filters). */
    fun forSlot(n: RawNotification, s: SlotCapture): Notif? {
        if (!s.active || skipReason(n) != null) return null
        if (s.smsDedupePkg.isNotEmpty() && AppGuard.normalize(n.pkg) == AppGuard.normalize(s.smsDedupePkg)) return null
        if (!mayShare(s.effective, n.pkg, s.serverApps)) return null
        if (AppGuard.isHidden(n.pkg, s.hidden)) return null
        var title = plain(n.title, TITLE_MAX)
        var text = plain(n.text, TEXT_MAX)
        var sub = plain(n.sub, SUB_MAX)
        if (s.hideCodes) { title = Redact.codes(title); text = Redact.codes(text); sub = Redact.codes(sub) }
        if (title.isEmpty() && text.isEmpty()) return null
        val label = plain(n.app, 60).ifEmpty { n.pkg }
        return Notif(AppGuard.normalize(n.pkg), label, title, text, sub, plain(n.category, 30), plain(n.key, 120), n.ts)
    }

    /** The last three lines of a chat notification, each with who said it. */
    fun chatTail(lines: List<Pair<String, String>>): String =
        lines.takeLast(3).joinToString(" / ") { (who, t) -> (if (who.isNotBlank()) "$who: " else "") + t }

    /** "Ring, Messages" for the sent log: app LABELS only, never any text. */
    fun labelsLine(items: List<Notif>): String {
        val labels = items.map { it.app }.distinct()
        return labels.take(4).joinToString(", ") + if (labels.size > 4) " +${labels.size - 4} more" else ""
    }
}

/** Notifications waiting to be sent to ONE connection. Deduped, bounded, oldest dropped first, and anything older than six hours is dropped rather than sent late. */
class CaptureOutbox(items: List<Notif> = emptyList()) {
    private val list = ArrayList<Notif>(items)

    @Synchronized fun add(n: Notif) {
        if (list.any { it.pkg == n.pkg && it.key == n.key && it.title == n.title && it.text == n.text }) return
        list.add(n)
        while (list.size > Capture.MAX_QUEUED) list.removeAt(0)
    }

    @Synchronized fun prune(now: Long) { list.removeAll { now - it.ts > Capture.MAX_AGE_MS } }
    @Synchronized fun peek(n: Int = Capture.MAX_PER_CALL): List<Notif> = list.take(minOf(n, Capture.MAX_PER_CALL))
    @Synchronized fun drop(count: Int) { repeat(minOf(count, list.size)) { list.removeAt(0) } }
    @Synchronized fun clear() { list.clear() }

    /** Keep only what [keep] accepts (turning texts off drops the queued texts and leaves notifications, and the other way round). */
    @Synchronized fun retain(keep: (Notif) -> Boolean) { list.retainAll { keep(it) } }
    @Synchronized fun size() = list.size
    @Synchronized fun body(): JSONObject = JSONObject().put("items", JSONArray(peek().map { it.toJson() }))
    @Synchronized fun serialize(): String = JSONArray(list.map { it.toJson() }).toString()

    companion object {
        fun parse(text: String?): CaptureOutbox = try {
            val a = JSONArray(if (text.isNullOrBlank()) "[]" else text)
            CaptureOutbox((0 until a.length()).mapNotNull { a.optJSONObject(it) }.mapNotNull { Notif.fromJson(it) })
        } catch (e: Exception) { CaptureOutbox() }
    }
}

/** The display-only list of apps that notified lately (label and package, never any content), so the owner can find a package name. Kept on the phone and never sent. */
class RecentApps(entries: List<Triple<String, String, Long>> = emptyList()) {
    private val items = ArrayList(entries)

    @Synchronized fun note(pkg: String, label: String, ts: Long) {
        val p = AppGuard.normalize(pkg)
        items.removeAll { it.first == p }
        items.add(0, Triple(p, label.take(60).ifEmpty { p }, ts))
        while (items.size > Capture.MAX_RECENT_APPS) items.removeAt(items.size - 1)
    }

    @Synchronized fun all(): List<Triple<String, String, Long>> = items.toList()
    @Synchronized fun serialize(): String = JSONArray(items.map { JSONObject().put("pkg", it.first).put("app", it.second).put("ts", it.third) }).toString()

    companion object {
        fun parse(text: String?): RecentApps = try {
            val a = JSONArray(if (text.isNullOrBlank()) "[]" else text)
            RecentApps((0 until a.length()).mapNotNull { a.optJSONObject(it) }.mapNotNull { o -> o.optString("pkg", "").takeIf { it.isNotEmpty() }?.let { Triple(it, o.optString("app", it), o.optLong("ts", 0L)) } }.take(Capture.MAX_RECENT_APPS))
        } catch (e: Exception) { RecentApps() }
    }
}

/** The owner-facing words for a level. */
object CaptureWords {
    fun level(l: CaptureLevel) = when (l) { CaptureLevel.OFF -> "Off"; CaptureLevel.APPS -> "Chosen apps"; CaptureLevel.ALL -> "Everything" }

    /** The sentence for incoming texts, which are a separate option from notifications. */
    fun sms(agent: String, server: Boolean, phone: Boolean, granted: Boolean): String = when {
        !phone -> "Nothing is shared with $agent: you have not turned this on."
        !server -> "Nothing is shared yet: the server has not switched text sharing on."
        !granted -> "Nothing is shared yet: Android's permission to receive texts is not granted."
        else -> "Sharing new incoming texts with $agent."
    }

    /** What is actually shared given both keys, in a sentence. */
    fun effective(agent: String, server: CaptureLevel, phone: CaptureLevel, access: Boolean, serverApps: Int): String {
        val eff = Capture.effective(server, phone)
        return when {
            phone == CaptureLevel.OFF -> "Nothing is shared with $agent: you have not turned this on."
            server == CaptureLevel.OFF -> "Nothing is shared yet: your setting is ${level(phone).lowercase()}, but the server has not switched this on (it allows nothing)."
            !access -> "Nothing is shared yet: Android's Notification access is not granted to this app."
            eff == CaptureLevel.APPS && serverApps == 0 -> "Nothing is shared yet: the server allows chosen apps but has not listed any."
            eff == CaptureLevel.APPS -> "Sharing the notifications of $serverApps chosen app${if (serverApps == 1) "" else "s"} with $agent."
            else -> "Sharing every notification (except private apps) with $agent."
        }
    }
}
