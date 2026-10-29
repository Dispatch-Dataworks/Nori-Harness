package app.nori.companion.core

/**
 * One phone can be paired with several Nori servers at once (for example two household assistants). Each pairing is a "connection" in a numbered slot.
 *
 * Slot 0 keeps the storage names the app always used, so a phone that was paired before connections existed keeps working with no migration. Slots 1.. get a prefix.
 * What belongs to ONE server (address, token, the cursor of messages already shown, what the server last said, health/place bookkeeping) is stored per slot.
 * What belongs to the PHONE (pause, the owner's switches, the control and screen windows, the hourly limits) is stored once and applies to every connection.
 */
object Connections {
    const val MAX = 4

    /** Slot used for requests that carry their own address and token (pairing). Never stored. */
    const val NONE = -1

    fun isSlot(slot: Int) = slot in 0 until MAX

    /** The storage name of a per-connection value. Slot 0 is the name unchanged (the pre-connections layout). */
    fun key(slot: Int, name: String): String = if (slot == 0) name else "c${slot}_$name"

    /** What a connection is called in the tab bar and in messages when the owner has not named it: the first part of its host, capitalised ("nori.example.com" -> "Nori"). */
    fun defaultLabel(host: String): String {
        val h = host.trim().lowercase()
        if (h.isEmpty()) return "Nori"
        if (h.all { it.isDigit() || it == '.' } || h.contains(':')) return h                       // an IP address has no name in it
        val first = h.substringBefore('.').ifEmpty { h }
        return first.replaceFirstChar { it.uppercase() }.take(20)
    }

    class Entry(val slot: Int, val label: String, val host: String)

    /** The names to show: each connection's own label, with the host added to any that would otherwise look the same. */
    fun displayNames(entries: List<Entry>): Map<Int, String> {
        val counts = entries.groupingBy { it.label.trim().lowercase() }.eachCount()
        return entries.associate { e ->
            val base = e.label.trim().ifEmpty { defaultLabel(e.host) }
            e.slot to if ((counts[e.label.trim().lowercase()] ?: 0) > 1 && e.host.isNotEmpty()) "$base (${e.host})" else base
        }
    }

    /** A saved connection, as far as pairing a new one is concerned. */
    class Saved(val slot: Int, val host: String, val hasToken: Boolean)

    enum class Kind { SAME_SERVER, REUSE_UNPAIRED, NEW, FULL }

    class Target(val slot: Int, val kind: Kind)

    /**
     * Where a pairing for [host] goes. The same server again replaces that connection's token (re-pairing). Otherwise a connection that was unpaired and left behind is reused,
     * so a typo does not pile up empty entries; otherwise the first free slot. Two servers on one host cannot be told apart by the web view (cookies are per host), so they are not kept apart either.
     */
    fun pairTarget(saved: List<Saved>, host: String): Target {
        val h = host.trim().lowercase()
        saved.firstOrNull { it.host.trim().lowercase() == h }?.let { return Target(it.slot, Kind.SAME_SERVER) }
        saved.firstOrNull { !it.hasToken }?.let { return Target(it.slot, Kind.REUSE_UNPAIRED) }
        val free = (0 until MAX).firstOrNull { s -> saved.none { it.slot == s } } ?: return Target(NONE, Kind.FULL)
        return Target(free, Kind.NEW)
    }

    /** The server address a link or a screen offers, only its host: used to compare servers. */
    fun hostOf(base: String): String = ServerUrl.host(base)

    /** Which connection a notification, widget or link wants. Falls back to [fallback] when it names none that exists. */
    fun pick(wanted: Int?, existing: List<Int>, fallback: Int): Int = when {
        wanted != null && wanted in existing -> wanted
        fallback in existing -> fallback
        else -> existing.firstOrNull() ?: 0
    }
}
