package app.nori.companion

import android.content.Context
import app.nori.companion.core.AgentName
import app.nori.companion.core.Connections
import app.nori.companion.core.DeviceSettings
import app.nori.companion.core.OwnerSwitches
import app.nori.companion.core.ServerUrl
import app.nori.companion.core.Windows

/**
 * Everything the app remembers, in one private file only this app can read. The password is never here: the website's own login handles it.
 *
 * Two kinds of value. What belongs to ONE Nori server is stored per connection slot (see [Connections]): its address, token, the cursor of messages already shown, what it last said
 * the phone may do. What belongs to the PHONE is stored once and covers every connection: pause, the owner's switches, the control and screen windows, the hourly limits.
 * Per-connection functions take the slot explicitly (no default), so nothing can quietly talk to the wrong server.
 */
object Prefs {
    private fun sp(c: Context) = c.applicationContext.getSharedPreferences("nori", Context.MODE_PRIVATE)
    private fun k(slot: Int, name: String) = Connections.key(slot, name)

    // ── connections ──────────────────────────────────────────────────
    /** Every connection that has an address (paired or not), in slot order. */
    fun slots(c: Context): List<Int> = (0 until Connections.MAX).filter { server(c, it).isNotEmpty() }

    /** Paired, whether or not the server still knows the phone. */
    fun pairedSlots(c: Context): List<Int> = slots(c).filter { isPaired(c, it) }

    /** Paired and not told to pair again: the connections that can be used (pause is checked separately, because it covers all of them). */
    fun usableSlots(c: Context): List<Int> = pairedSlots(c).filter { !needsRepair(c, it) }

    fun anyPaired(c: Context) = pairedSlots(c).isNotEmpty()

    fun server(c: Context, slot: Int): String = sp(c).getString(k(slot, "server"), "") ?: ""
    fun token(c: Context, slot: Int): String = sp(c).getString(k(slot, "token"), "") ?: ""
    fun deviceName(c: Context, slot: Int): String = sp(c).getString(k(slot, "name"), "") ?: ""
    fun isPaired(c: Context, slot: Int) = token(c, slot).isNotEmpty() && server(c, slot).isNotEmpty()
    fun host(c: Context, slot: Int) = ServerUrl.host(server(c, slot))

    /** What the owner calls this connection (tab title), or the first part of the server's host. */
    fun label(c: Context, slot: Int): String = sp(c).getString(k(slot, "label"), "")?.trim().orEmpty()
        .ifEmpty { assistantName(c, slot) }.ifEmpty { Connections.defaultLabel(host(c, slot)) }

    /** What the server says its assistant is called (empty from a server that does not say, or before the first poll). */
    fun assistantName(c: Context, slot: Int): String = serverSettings(c, slot).assistantName

    /** The assistant of one connection, by name: what the server calls it, else what this connection is called. */
    fun agent(c: Context, slot: Int): String = assistantName(c, slot).ifEmpty { label(c, slot) }

    /** The assistant(s) of the usable connections, for text that is not about one in particular ("Iron", "Nori and Iron", or "your assistant" when none). */
    fun agent(c: Context): String = AgentName.join(usableSlots(c).map { agent(c, it) })
    fun setLabel(c: Context, slot: Int, v: String) = sp(c).edit().putString(k(slot, "label"), v.trim().take(20)).apply()

    /** Labels to show people: made distinct when two connections would otherwise read the same. */
    fun names(c: Context): Map<Int, String> = Connections.displayNames(slots(c).map { Connections.Entry(it, label(c, it), host(c, it)) })
    fun name(c: Context, slot: Int): String = names(c)[slot] ?: label(c, slot)

    /** The connection the owner last looked at (the tab that was in front). Widgets and shortcuts that name none go here. */
    fun selected(c: Context): Int = Connections.pick(null, slots(c), sp(c).getInt("selected_conn", 0))
    fun setSelected(c: Context, slot: Int) = sp(c).edit().putInt("selected_conn", slot).apply()

    /** The connection the Share sheet used last, so the next share defaults to it. */
    fun lastShare(c: Context): Int = sp(c).getInt("last_share_conn", -1)
    fun setLastShare(c: Context, slot: Int) = sp(c).edit().putInt("last_share_conn", slot).apply()

    /** The server answered 401: this pairing is gone. The app stops talking to that server until it is paired again. */
    fun needsRepair(c: Context, slot: Int) = sp(c).getBoolean(k(slot, "needs_repair"), false)
    fun setNeedsRepair(c: Context, slot: Int, v: Boolean) = sp(c).edit().putBoolean(k(slot, "needs_repair"), v).apply()

    private val PER_CONNECTION_STATE = listOf("ack", "seen", "settings", "health_ledger", "place_tracker", "event_outbox", "current_place", "status", "latest_app", "apk_url")

    fun setPaired(c: Context, slot: Int, server: String, token: String, name: String) {
        val others = pairedSlots(c).any { it != slot }
        val e = sp(c).edit()
        e.putString(k(slot, "server"), server.trim().trimEnd('/')).putString(k(slot, "token"), token).putString(k(slot, "name"), name).putBoolean(k(slot, "needs_repair"), false)
        e.putLong(k(slot, "ack"), 0L)
        for (n in PER_CONNECTION_STATE) if (n != "ack") e.putString(k(slot, n), "")
        if (!others) {
            // the first pairing starts from a clean phone; adding one more must not undo a pause or close a window the owner opened for the others
            e.putBoolean("paused", false).putString("cap_actions", "").putString("cap_interact", "").putLong("interact_until", 0L).putLong("share_until", 0L)
        }
        e.apply()
    }

    /** Forget the pairing but keep the address (so it can be paired again with a new code). */
    fun unpair(c: Context, slot: Int) {
        val e = sp(c).edit()
        e.remove(k(slot, "token")).remove(k(slot, "name")).putBoolean(k(slot, "needs_repair"), false)
        for (n in listOf("settings", "current_place", "health_ledger", "place_tracker", "event_outbox", "status")) e.putString(k(slot, n), "")
        e.apply()
        if (!anyPaired(c)) sp(c).edit().putLong("interact_until", 0L).putLong("share_until", 0L).apply()
    }

    /** Forget the connection altogether (address, label and everything kept for it). Slot 0 is only emptied, never renumbered. */
    fun removeConnection(c: Context, slot: Int) {
        unpair(c, slot)
        val e = sp(c).edit()
        for (n in listOf("server", "label", "needs_repair", "ack", "seen", "latest_app", "apk_url")) e.remove(k(slot, n))
        e.apply()
        if (sp(c).getInt("selected_conn", 0) == slot) sp(c).edit().remove("selected_conn").apply()
        if (lastShare(c) == slot) sp(c).edit().remove("last_share_conn").apply()
    }

    // ── pause ────────────────────────────────────────────────────────
    fun paused(c: Context) = sp(c).getBoolean("paused", false)
    fun setPaused(c: Context, v: Boolean) = sp(c).edit().putBoolean("paused", v).apply()

    /** A connection does anything at all only when paired, not removed on the server, and the phone is not paused. */
    fun active(c: Context, slot: Int) = isPaired(c, slot) && !needsRepair(c, slot) && !paused(c)

    // ── the owner's own switches (the phone's half of every two-key capability; they apply to every connection) ──
    fun ownerNotifications(c: Context) = sp(c).getBoolean("o_notifications", true)
    fun ownerShare(c: Context) = sp(c).getBoolean("o_share", true)
    fun ownerHealth(c: Context) = sp(c).getBoolean("o_health", false)
    fun ownerPlaces(c: Context) = sp(c).getBoolean("o_places", false)
    fun ownerActions(c: Context) = sp(c).getBoolean("o_actions", false)
    fun ownerScreen(c: Context) = sp(c).getBoolean("o_screen", false)
    fun ownerControl(c: Context) = sp(c).getBoolean("o_control", false)
    fun setOwner(c: Context, key: String, v: Boolean) = sp(c).edit().putBoolean("o_$key", v).apply()
    fun owner(c: Context) = OwnerSwitches(ownerNotifications(c), ownerShare(c), ownerHealth(c), ownerPlaces(c), ownerActions(c), ownerScreen(c), ownerControl(c))

    /** Per-metric health consent: each metric is off until the owner switches it on. */
    fun healthMetrics(c: Context): Set<String> = sp(c).getStringSet("health_metrics", emptySet()) ?: emptySet()
    fun setHealthMetric(c: Context, metric: String, on: Boolean) {
        val s = healthMetrics(c).toMutableSet()
        if (on) s.add(metric) else s.remove(metric)
        sp(c).edit().putStringSet("health_metrics", s).apply()
    }

    // ── what each server last said ───────────────────────────────────
    fun serverSettings(c: Context, slot: Int): DeviceSettings = DeviceSettings.parse(sp(c).getString(k(slot, "settings"), ""))
    fun serverSettingsRaw(c: Context, slot: Int): String = sp(c).getString(k(slot, "settings"), "") ?: ""
    fun setServerSettings(c: Context, slot: Int, json: String) = sp(c).edit().putString(k(slot, "settings"), json).apply()

    // ── windows (epoch ms; 0 = closed). One window covers every connection: it is the phone's, opened by the owner on the phone ──
    fun interactUntil(c: Context) = sp(c).getLong("interact_until", 0L)
    fun setInteractUntil(c: Context, ms: Long) = sp(c).edit().putLong("interact_until", ms).apply()
    fun shareUntil(c: Context) = sp(c).getLong("share_until", 0L)
    fun setShareUntil(c: Context, ms: Long) = sp(c).edit().putLong("share_until", ms).apply()
    fun interactOpen(c: Context, now: Long = System.currentTimeMillis()) = Windows.isOpen(interactUntil(c), now)
    fun shareOpen(c: Context, now: Long = System.currentTimeMillis()) = Windows.isOpen(shareUntil(c), now)

    /** The length the quick tiles use when they open a window (15 min, 1 h or 3 h). */
    fun interactMinutes(c: Context) = sp(c).getInt("interact_minutes", 15).let { if (Windows.isValidMinutes(it)) it else 15 }
    fun setInteractMinutes(c: Context, m: Int) = sp(c).edit().putInt("interact_minutes", m).apply()
    fun shareMinutes(c: Context) = sp(c).getInt("share_minutes", 15).let { if (Windows.isValidMinutes(it)) it else 15 }
    fun setShareMinutes(c: Context, m: Int) = sp(c).edit().putInt("share_minutes", m).apply()

    // ── notification cursor (per connection: each server numbers its own messages) ──
    fun ack(c: Context, slot: Int) = sp(c).getLong(k(slot, "ack"), 0L)
    fun seen(c: Context, slot: Int) = sp(c).getString(k(slot, "seen"), "") ?: ""
    fun setInbox(c: Context, slot: Int, ack: Long, seen: String) = sp(c).edit().putLong(k(slot, "ack"), ack).putString(k(slot, "seen"), seen).apply()

    // ── persisted state of the decision objects ──────────────────────
    /** Phone-wide values (the hourly limits). */
    fun get(c: Context, key: String): String = sp(c).getString(key, "") ?: ""
    fun put(c: Context, key: String, v: String) = sp(c).edit().putString(key, v).apply()

    /** Values that belong to one connection (the health ledger, the place tracker and outbox). */
    fun getFor(c: Context, slot: Int, key: String): String = get(c, k(slot, key))
    fun putFor(c: Context, slot: Int, key: String, v: String) = put(c, k(slot, key), v)

    fun currentPlace(c: Context, slot: Int) = getFor(c, slot, "current_place")
    fun setCurrentPlace(c: Context, slot: Int, name: String) = putFor(c, slot, "current_place", name)

    // ── updates and status ───────────────────────────────────────────
    fun latestApp(c: Context, slot: Int) = getFor(c, slot, "latest_app")
    fun setLatestApp(c: Context, slot: Int, v: String) = putFor(c, slot, "latest_app", v)
    fun apkUrl(c: Context, slot: Int) = getFor(c, slot, "apk_url")
    fun setApkUrl(c: Context, slot: Int, v: String) = putFor(c, slot, "apk_url", v)

    /** The newest version the owner was already told about (one notice per version, however many servers offer it). */
    fun updateNotified(c: Context) = get(c, "update_notified")
    fun setUpdateNotified(c: Context, v: String) = put(c, "update_notified", v)

    fun status(c: Context, slot: Int) = getFor(c, slot, "status")
    fun setStatus(c: Context, slot: Int, v: String) = putFor(c, slot, "status", v)

    /** One-time prompts. */
    fun flag(c: Context, key: String) = sp(c).getBoolean("flag_$key", false)
    fun setFlag(c: Context, key: String, v: Boolean = true) = sp(c).edit().putBoolean("flag_$key", v).apply()

    fun keepAwake(c: Context) = sp(c).getBoolean("keep_awake", true)
    fun setKeepAwake(c: Context, v: Boolean) = sp(c).edit().putBoolean("keep_awake", v).apply()
}

/**
 * Whether the Nori screen is in front (the server then holds back notifications, because the owner is looking at the chat), and which connection's tab is showing:
 * only that server is told the chat is visible, the other still sends its notifications.
 */
object AppState {
    @Volatile var appVisible = false
    @Volatile var visibleSlot = -1

    fun chatVisibleFor(slot: Int) = appVisible && visibleSlot == slot
}
