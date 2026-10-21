package app.nori.companion

import android.content.Context
import app.nori.companion.core.DeviceSettings
import app.nori.companion.core.OwnerSwitches
import app.nori.companion.core.Windows

/** Everything the app remembers, in one private file only this app can read. The password is never here: the website's own login handles it. */
object Prefs {
    private fun sp(c: Context) = c.applicationContext.getSharedPreferences("nori", Context.MODE_PRIVATE)

    // ── pairing ──────────────────────────────────────────────────────
    fun server(c: Context): String = sp(c).getString("server", "") ?: ""
    fun setServer(c: Context, v: String) = sp(c).edit().putString("server", v.trim().trimEnd('/')).apply()
    fun token(c: Context): String = sp(c).getString("token", "") ?: ""
    fun deviceName(c: Context): String = sp(c).getString("name", "") ?: ""
    fun isPaired(c: Context) = token(c).isNotEmpty() && server(c).isNotEmpty()

    /** The server answered 401: the pairing is gone. The app stops talking until it is paired again. */
    fun needsRepair(c: Context) = sp(c).getBoolean("needs_repair", false)
    fun setNeedsRepair(c: Context, v: Boolean) = sp(c).edit().putBoolean("needs_repair", v).apply()

    fun setPaired(c: Context, server: String, token: String, name: String) {
        sp(c).edit().putString("server", server.trim().trimEnd('/')).putString("token", token).putString("name", name).putBoolean("needs_repair", false).putBoolean("paused", false)
            .putLong("ack", 0L).putString("seen", "").putString("settings", "").putString("health_ledger", "").putString("place_tracker", "").putString("event_outbox", "")
            .putString("cap_actions", "").putString("cap_interact", "").putLong("interact_until", 0L).putLong("share_until", 0L).putString("current_place", "").apply()
    }

    fun unpair(c: Context) {
        sp(c).edit().remove("token").remove("name").putBoolean("needs_repair", false).putLong("interact_until", 0L).putLong("share_until", 0L).putString("settings", "")
            .putString("current_place", "").putString("health_ledger", "").putString("place_tracker", "").putString("event_outbox", "").apply()
    }

    // ── pause ────────────────────────────────────────────────────────
    fun paused(c: Context) = sp(c).getBoolean("paused", false)
    fun setPaused(c: Context, v: Boolean) = sp(c).edit().putBoolean("paused", v).apply()

    /** The app does anything at all only when paired, not removed on the server, and not paused. */
    fun active(c: Context) = isPaired(c) && !needsRepair(c) && !paused(c)

    // ── the owner's own switches (the phone's half of every two-key capability) ──
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

    // ── what the server last said ────────────────────────────────────
    fun serverSettings(c: Context): DeviceSettings = DeviceSettings.parse(sp(c).getString("settings", ""))
    fun serverSettingsRaw(c: Context): String = sp(c).getString("settings", "") ?: ""
    fun setServerSettings(c: Context, json: String) = sp(c).edit().putString("settings", json).apply()

    // ── windows (epoch ms; 0 = closed) ───────────────────────────────
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

    // ── notification cursor ──────────────────────────────────────────
    fun ack(c: Context) = sp(c).getLong("ack", 0L)
    fun seen(c: Context) = sp(c).getString("seen", "") ?: ""
    fun setInbox(c: Context, ack: Long, seen: String) = sp(c).edit().putLong("ack", ack).putString("seen", seen).apply()

    // ── persisted state of the decision objects ──────────────────────
    fun get(c: Context, key: String): String = sp(c).getString(key, "") ?: ""
    fun put(c: Context, key: String, v: String) = sp(c).edit().putString(key, v).apply()

    fun currentPlace(c: Context) = get(c, "current_place")
    fun setCurrentPlace(c: Context, name: String) = put(c, "current_place", name)

    // ── updates and status ───────────────────────────────────────────
    fun latestApp(c: Context) = get(c, "latest_app")
    fun setLatestApp(c: Context, v: String) = put(c, "latest_app", v)
    fun apkUrl(c: Context) = get(c, "apk_url")
    fun setApkUrl(c: Context, v: String) = put(c, "apk_url", v)
    fun updateNotified(c: Context) = get(c, "update_notified")
    fun setUpdateNotified(c: Context, v: String) = put(c, "update_notified", v)

    fun status(c: Context) = get(c, "status")
    fun setStatus(c: Context, v: String) = put(c, "status", v)

    /** One-time prompts. */
    fun flag(c: Context, key: String) = sp(c).getBoolean("flag_$key", false)
    fun setFlag(c: Context, key: String, v: Boolean = true) = sp(c).edit().putBoolean("flag_$key", v).apply()

    fun keepAwake(c: Context) = sp(c).getBoolean("keep_awake", true)
    fun setKeepAwake(c: Context, v: Boolean) = sp(c).edit().putBoolean("keep_awake", v).apply()
}

/** Whether the Nori screen is in front (the server then holds back notifications, because the owner is looking at the chat). */
object AppState {
    @Volatile var appVisible = false
}
