package app.nori.companion

import android.Manifest
import android.content.ComponentName
import android.content.Context
import android.content.pm.PackageManager
import android.provider.Settings
import android.provider.Telephony
import app.nori.companion.core.Caps
import app.nori.companion.core.Capture
import app.nori.companion.core.CaptureLevel
import app.nori.companion.core.Notif
import app.nori.companion.core.PollAction
import app.nori.companion.core.RawNotification
import app.nori.companion.core.SlotCapture
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.ConcurrentHashMap

/**
 * Sharing the phone's notifications and incoming texts, one connection at a time. Everything is off until the owner turns it on for THAT connection and the connection's server allows it too
 * (see core/Capture.kt for the rules, which are unit tested). What is shared waits in a small per-connection outbox, goes out in batches from the quiet worker, and is dropped (never sent late)
 * after six hours or the moment the owner switches the connection off, pauses the phone, or unpairs. The sent log gets counts and app labels, never any text.
 */
object CaptureSync {
    private val lock = Any()
    private val labels = ConcurrentHashMap<String, String>()
    private val failing = ConcurrentHashMap.newKeySet<Int>()

    fun smsGranted(c: Context) = c.checkSelfPermission(Manifest.permission.RECEIVE_SMS) == PackageManager.PERMISSION_GRANTED

    /** Whether Android's "Notification access" is on for this app (the owner's act, in system settings). */
    fun accessGranted(c: Context): Boolean {
        val flat = Settings.Secure.getString(c.contentResolver, "enabled_notification_listeners") ?: return false
        return flat.split(":").any { ComponentName.unflattenFromString(it)?.packageName == c.packageName }
    }

    /** One connection's two keys (and the text switch), read from what the phone remembers. */
    fun slotState(c: Context, slot: Int, smsGranted: Boolean = smsGranted(c)): SlotCapture {
        val s = Prefs.serverSettings(c, slot)
        val smsOn = s.captureSms && Prefs.captureSms(c, slot) && smsGranted
        val dedupe = if (smsOn) try { Telephony.Sms.getDefaultSmsPackage(c) ?: "" } catch (e: Exception) { "" } else ""
        return SlotCapture(slot, Prefs.active(c, slot), s.capture, Prefs.captureLevel(c, slot), s.captureApps, s.hiddenApps, s.hideCodes, s.captureSms, Prefs.captureSms(c, slot), s.captureSmsShort, dedupe)
    }

    fun slotStates(c: Context): List<SlotCapture> { val g = smsGranted(c); return Prefs.slots(c).map { slotState(c, it, g) } }

    /** The caps one connection reports: the shared device caps plus its own `capture` and `sms` flags. */
    fun capsFor(c: Context, slot: Int, base: Caps): Caps =
        base.copy(capture = Capture.capsFlag(Prefs.captureLevel(c, slot), accessGranted(c)), sms = Capture.smsCapsFlag(Prefs.captureSms(c, slot), smsGranted(c)))

    /** Cheap first check for the listener: is any connection set to share notifications at all (this phone's half)? */
    fun listenerWanted(c: Context): Boolean = !Prefs.paused(c) && Prefs.slots(c).any { Prefs.active(c, it) && Prefs.captureLevel(c, it) != CaptureLevel.OFF }

    fun labelFor(c: Context, pkg: String): String = labels.getOrPut(pkg) {
        try { c.packageManager.getApplicationLabel(c.packageManager.getApplicationInfo(pkg, 0)).toString() } catch (e: Exception) { pkg }
    }

    private fun enqueue(c: Context, slot: Int, n: Notif) = synchronized(lock) {
        val o = Prefs.captureOutbox(c, slot)
        o.prune(System.currentTimeMillis())
        o.add(n)
        Prefs.saveCaptureOutbox(c, slot, o)
    }

    /** A notification arrived (called by the listener; never throws). */
    fun offer(c: Context, raw: RawNotification) {
        try {
            if (!listenerWanted(c)) return
            val slots = slotStates(c)
            if (!Capture.anyPhoneLevelOn(slots)) return
            if (Capture.skipReason(raw) == null) noteRecent(c, raw)
            var queued = false
            for (s in slots) Capture.forSlot(raw, s)?.let { enqueue(c, s.slot, it); queued = true }
            if (queued) SyncService.nudgeCapture(c)
        } catch (e: Exception) { /* reading a notification must never break anything */ }
    }

    /** An incoming text arrived (called by the SMS receiver; never throws). */
    fun offerSms(c: Context, address: String, body: String, ts: Long) {
        try {
            if (Prefs.paused(c)) return
            if (Prefs.slots(c).none { Prefs.captureSms(c, it) }) return
            val granted = smsGranted(c)
            var queued = false
            for (s in slotStates(c)) Capture.smsFor(address, body, ts, s, granted)?.let { enqueue(c, s.slot, it); queued = true }
            if (queued) SyncService.nudgeCapture(c)
        } catch (e: Exception) { /* nothing */ }
    }

    private fun noteRecent(c: Context, raw: RawNotification) {
        val r = Prefs.recentNotifApps(c)
        val first = r.all().firstOrNull()
        if (first != null && first.first == raw.pkg.lowercase() && raw.ts - first.third < 60_000L) return
        r.note(raw.pkg, labelFor(c, raw.pkg), raw.ts)
        Prefs.saveRecentNotifApps(c, r)
    }

    /** Drop what a connection no longer allows: everything while paused or not usable, notifications when its level is off, texts when its text switch is off. Safe to call as often as you like. */
    fun reconcile(c: Context) {
        val granted = smsGranted(c)
        for (slot in Prefs.slots(c)) {
            if (Prefs.captureOutbox(c, slot).size() == 0) continue
            val s = slotState(c, slot, granted)
            synchronized(lock) {
                val o = Prefs.captureOutbox(c, slot)
                if (!s.active) o.clear()
                else {
                    val notifOk = s.effective != CaptureLevel.OFF
                    val smsOk = Capture.smsEffective(s, granted)
                    o.retain { if (it.pkg == Capture.SMS_PKG) smsOk else notifOk }
                }
                Prefs.saveCaptureOutbox(c, slot, o)
            }
        }
    }

    /** Send what waits for one connection, in batches of at most 60. 403 = the server does not take it: drop the queue. 401 is left to the poll loop (it asks to pair again). Anything else keeps the queue for the next try. */
    fun flush(c: Context, slot: Int) {
        if (!Prefs.active(c, slot)) return
        val agent = Prefs.agent(c, slot)
        while (true) {
            val batch: List<Notif> = synchronized(lock) {
                val o = Prefs.captureOutbox(c, slot)
                o.prune(System.currentTimeMillis())
                Prefs.saveCaptureOutbox(c, slot, o)
                o.peek()
            }
            if (batch.isEmpty()) return
            val r = Api.post(c, slot, "/device/notifications", JSONObject().put("items", JSONArray(batch.map { it.toJson() })))
            if (r.ok) {
                failing.remove(slot)
                synchronized(lock) { val o = Prefs.captureOutbox(c, slot); o.drop(batch.size); Prefs.saveCaptureOutbox(c, slot, o) }
                val texts = batch.count { it.pkg == Capture.SMS_PKG }
                val notifs = batch.filter { it.pkg != Capture.SMS_PKG }
                if (notifs.isNotEmpty()) SentLog.add(c, "shared ${notifs.size} notification${if (notifs.size == 1) "" else "s"} with $agent (${Capture.labelsLine(notifs)})")
                if (texts > 0) SentLog.add(c, "shared $texts text message${if (texts == 1) "" else "s"} with $agent")
                continue
            }
            if (r.action == PollAction.RE_PAIR) return
            if (r.code == 403) {
                synchronized(lock) { Prefs.clearCaptureOutbox(c, slot) }
                SentLog.add(c, "$agent does not take shared notifications or texts (switched off on the server): dropped ${batch.size} waiting")
            } else if (failing.add(slot)) {
                SentLog.add(c, "could not share with $agent yet (${r.error}): ${batch.size} wait, for up to 6 hours")
            }
            return
        }
    }
}
