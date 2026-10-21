package app.nori.companion

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService
import app.nori.companion.core.Windows

/** The two owner-opened windows (control, screen share) and the one-tap pause: the logic behind the tiles, notification buttons and settings screen. */
object WindowControl {
    private val main = Handler(Looper.getMainLooper())
    private var expiry: Runnable? = null

    /** Why the control window cannot be opened now, or null when it can. */
    fun interactBlocker(c: Context): String? {
        if (!Prefs.isPaired(c) || Prefs.needsRepair(c)) return "Pair this phone with Nori first."
        if (Prefs.paused(c)) return "Nori is paused: resume it first."
        if (!Prefs.ownerControl(c)) return "Switch on \"Let Nori control this phone\" in the Nori app first."
        if (!NoriAccessibilityService.isEnabled(c)) return "Turn on the Nori accessibility service in Android's settings first."
        return null
    }

    fun shareBlocker(c: Context): String? {
        if (!Prefs.isPaired(c) || Prefs.needsRepair(c)) return "Pair this phone with Nori first."
        if (Prefs.paused(c)) return "Nori is paused: resume it first."
        if (!Prefs.ownerScreen(c)) return "Switch on \"Let Nori look at this screen\" in the Nori app first."
        if (!NoriAccessibilityService.isEnabled(c)) return "Turn on the Nori accessibility service first: it is how the phone knows which app is in front, so private apps are never captured."
        return null
    }

    /** Opens the control window for [minutes] (15, 60 or 180). Only ever called from something the owner touched on the phone. Returns an error text or null. */
    fun openInteract(c: Context, minutes: Int): String? {
        interactBlocker(c)?.let { return it }
        val now = System.currentTimeMillis()
        val until = Windows.open(now, minutes) ?: return "Choose 15 minutes, 1 hour or 3 hours."
        Prefs.setInteractUntil(c, until)
        SentLog.add(c, "control window OPENED by you for ${Windows.label(minutes)}")
        KeepAwake.hold(c, "interact", until)
        Notifier.showInteractWindow(c, until)
        Notifier.refreshSync(c)
        scheduleExpiry(c, until)
        SyncService.pollNow(c)
        TileRefresh.all(c)
        return null
    }

    fun closeInteract(c: Context, why: String = "you closed it") {
        if (Prefs.interactUntil(c) > 0L) SentLog.add(c, "control window CLOSED ($why)")
        Prefs.setInteractUntil(c, 0L)
        KeepAwake.release(c, "interact")
        Notifier.cancelInteractWindow(c)
        Notifier.refreshSync(c)
        SyncService.pollNow(c)
        TileRefresh.all(c)
    }

    private fun scheduleExpiry(c: Context, until: Long) {
        val app = c.applicationContext
        main.post {
            expiry?.let { main.removeCallbacks(it) }
            val r = Runnable { reconcile(app) }
            expiry = r
            main.postDelayed(r, (until - System.currentTimeMillis()).coerceAtLeast(0L) + 500L)
        }
    }

    /** Closes any window whose time has run out (also called by the service every cycle, so a restart cannot leave one open). */
    fun reconcile(c: Context) {
        val now = System.currentTimeMillis()
        if (Prefs.interactUntil(c) > 0L && !Windows.isOpen(Prefs.interactUntil(c), now)) closeInteract(c, "the time ran out")
        if (Prefs.shareUntil(c) > 0L && !Windows.isOpen(Prefs.shareUntil(c), now) && !ScreenShareService.active()) {
            Prefs.setShareUntil(c, 0L)
            SentLog.add(c, "screen-share window CLOSED (the time ran out)")
            SyncService.pollNow(c)
            TileRefresh.all(c)
        }
    }

    /** After a restart the control window resumes its timer (and its notice) if it is still open. */
    fun resume(c: Context) {
        val until = Prefs.interactUntil(c)
        if (Windows.isOpen(until, System.currentTimeMillis())) {
            if (!NoriAccessibilityService.isEnabled(c) || Prefs.paused(c)) { closeInteract(c, "the accessibility service or the app is off"); return }
            Notifier.showInteractWindow(c, until)
            KeepAwake.hold(c, "interact", until)
            scheduleExpiry(c, until)
        } else reconcile(c)
    }
}

object Control {
    /** One switch that stops everything at once (the Quick Settings tile, the notification button, the settings screen). */
    fun setPaused(c: Context, paused: Boolean) {
        Prefs.setPaused(c, paused)
        SentLog.add(c, if (paused) "PAUSED: nothing is sent except a heartbeat" else "resumed")
        if (paused) {
            WindowControl.closeInteract(c, "paused")
            ScreenShareService.stop(c)
            Api.cancelPoll()
        }
        PlaceManager.sync(c)
        Notifier.refreshSync(c)
        SyncService.pollNow(c)
        TileRefresh.all(c)
    }
}

class ControlReceiver : BroadcastReceiver() {
    override fun onReceive(c: Context, i: Intent) {
        when (i.action) {
            PAUSE -> Control.setPaused(c, true)
            RESUME -> Control.setPaused(c, false)
            STOP_SHARE -> ScreenShareService.stop(c)
            CLOSE_INTERACT -> WindowControl.closeInteract(c)
        }
    }

    companion object {
        const val PAUSE = "app.nori.companion.PAUSE"
        const val RESUME = "app.nori.companion.RESUME"
        const val STOP_SHARE = "app.nori.companion.STOP_SHARE"
        const val CLOSE_INTERACT = "app.nori.companion.CLOSE_INTERACT"
    }
}

/** Starts the connection at boot and after an update. Windows never survive a restart of the phone. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(c: Context, i: Intent) {
        if (i.action != Intent.ACTION_BOOT_COMPLETED && i.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        if (i.action == Intent.ACTION_BOOT_COMPLETED) {
            Prefs.setInteractUntil(c, 0L)
            Prefs.setShareUntil(c, 0L)
        }
        if (Prefs.isPaired(c)) SyncService.start(c)          // the service re-registers the place fences and the health schedule
    }
}

object TileRefresh {
    fun all(c: Context) {
        for (cls in listOf(PauseTile::class.java, InteractTile::class.java, ShareTile::class.java)) {
            try { TileService.requestListeningState(c, ComponentName(c, cls)) } catch (e: Exception) { /* tile not added */ }
        }
    }
}

private fun TileService.openApp(cls: Class<*>, req: Int, vararg extras: Pair<String, Int>) {
    val i = Intent(this, cls).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    for ((k, v) in extras) i.putExtra(k, v)
    if (Build.VERSION.SDK_INT >= 34) startActivityAndCollapse(PendingIntent.getActivity(this, req, i, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT))
    else @Suppress("DEPRECATION") startActivityAndCollapse(i)
}

/** Quick Settings tile: pause or resume everything in one tap. */
class PauseTile : TileService() {
    override fun onStartListening() { refresh() }
    override fun onClick() {
        if (!Prefs.isPaired(this)) { openApp(SettingsActivity::class.java, 20); return }
        Control.setPaused(this, !Prefs.paused(this))
        refresh()
    }

    private fun refresh() {
        val t = qsTile ?: return
        t.label = "Nori"
        if (!Prefs.isPaired(this)) { t.state = Tile.STATE_UNAVAILABLE; t.subtitle = "Not paired" }
        else if (Prefs.paused(this)) { t.state = Tile.STATE_INACTIVE; t.subtitle = "Paused" }
        else { t.state = Tile.STATE_ACTIVE; t.subtitle = "On" }
        t.updateTile()
    }
}

/** Quick Settings tile: open (or close) the control window, for the length chosen in the app (15 min by default). The window can only be opened here, on the phone. */
class InteractTile : TileService() {
    override fun onStartListening() { refresh() }
    override fun onClick() {
        if (Prefs.interactOpen(this)) { WindowControl.closeInteract(this); refresh(); return }
        val problem = WindowControl.interactBlocker(this)
        if (problem != null) { openApp(SettingsActivity::class.java, 21); return }
        WindowControl.openInteract(this, Prefs.interactMinutes(this))
        refresh()
    }

    private fun refresh() {
        val t = qsTile ?: return
        val open = Prefs.interactOpen(this)
        t.label = "Nori may act"
        t.state = if (open) Tile.STATE_ACTIVE else Tile.STATE_INACTIVE
        t.subtitle = if (open) Windows.countdown(Windows.remainingMs(Prefs.interactUntil(this), System.currentTimeMillis())) else "Closed"
        t.updateTile()
    }
}

/** Quick Settings tile: open (or close) the screen-share window. Android asks for its own screen-capture consent first. */
class ShareTile : TileService() {
    override fun onStartListening() { refresh() }
    override fun onClick() {
        if (Prefs.shareOpen(this) && ScreenShareService.active()) { ScreenShareService.stop(this); refresh(); return }
        openApp(ScreenShareRequestActivity::class.java, 22, ScreenShareRequestActivity.EXTRA_MINUTES to Prefs.shareMinutes(this))
    }

    private fun refresh() {
        val t = qsTile ?: return
        val open = Prefs.shareOpen(this) && ScreenShareService.active()
        t.label = "Nori may look"
        t.state = if (open) Tile.STATE_ACTIVE else Tile.STATE_INACTIVE
        t.subtitle = if (open) Windows.countdown(Windows.remainingMs(Prefs.shareUntil(this), System.currentTimeMillis())) else "Closed"
        t.updateTile()
    }
}
