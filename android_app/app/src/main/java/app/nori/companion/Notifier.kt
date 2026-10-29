package app.nori.companion

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import app.nori.companion.core.Channels
import app.nori.companion.core.Deeplink
import app.nori.companion.core.Message
import app.nori.companion.core.ShowOutcome
import app.nori.companion.core.Windows

/** The notifications this app shows: Nori's messages (the category picks the channel), the small ongoing one with a Pause button, and the window countdowns. */
object Notifier {
    const val ID_SYNC = 1
    const val ID_INTERACT = 2
    const val ID_SHARE = 3
    const val ID_UPDATE = 4

    private const val IMMUTABLE_UPDATE = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE

    fun channels(c: Context) {
        val nm = c.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        for (s in Channels.ALL) {
            val ch = NotificationChannel(s.id, s.name, s.importance).apply {
                description = s.description
                if (s.importance <= Channels.IMPORTANCE_LOW) setShowBadge(false)
            }
            nm.createNotificationChannel(ch)
        }
    }

    fun canNotify(c: Context): Boolean =
        (Build.VERSION.SDK_INT < 33 || ContextCompat.checkSelfPermission(c, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) &&
            NotificationManagerCompat.from(c).areNotificationsEnabled()

    /** Tapping a notification opens the web view at the chat of the connection it came from (its tab). */
    fun openChat(c: Context, requestCode: Int = 0, slot: Int = Prefs.selected(c)): PendingIntent =
        PendingIntent.getActivity(c, requestCode, Intent(Intent.ACTION_VIEW, Uri.parse(Deeplink.chatLink(slot))).setClass(c, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP), IMMUTABLE_UPDATE)

    private fun openSettings(c: Context, requestCode: Int): PendingIntent =
        PendingIntent.getActivity(c, requestCode, Intent(c, SettingsActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK), IMMUTABLE_UPDATE)

    private fun control(c: Context, action: String, req: Int): PendingIntent =
        PendingIntent.getBroadcast(c, req, Intent(c, ControlReceiver::class.java).setAction(action), IMMUTABLE_UPDATE)

    // ── the ongoing "connected" notice (the foreground service's own) ─
    fun syncNotification(c: Context): Notification {
        val paused = Prefs.paused(c)
        val needRepair = Prefs.pairedSlots(c).filter { Prefs.needsRepair(c, it) }
        val repair = needRepair.isNotEmpty()
        val several = Prefs.pairedSlots(c).size > 1
        val b = NotificationCompat.Builder(c, Channels.SYNC)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle(if (paused) "Nori is paused" else if (repair) (if (several) needRepair.joinToString(", ") { Prefs.name(c, it) } + " needs pairing again" else "Nori needs pairing again") else "Nori is connected")
            .setContentText(if (paused) "Nothing is sent except a heartbeat" else if (repair) "Tap to open the app settings" else "Replies and reminders arrive here. Tap to open.")
            .setOngoing(true).setOnlyAlertOnce(true).setShowWhen(false)
            .setContentIntent(if (repair) openSettings(c, 10) else openChat(c, 11))
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
        if (paused) b.addAction(0, "Resume", control(c, ControlReceiver.RESUME, 1)) else b.addAction(0, "Pause", control(c, ControlReceiver.PAUSE, 2))
        b.addAction(0, "Settings", openSettings(c, 4))
        return b.build()
    }

    fun refreshSync(c: Context) {
        if (!canNotify(c)) return
        try { NotificationManagerCompat.from(c).notify(ID_SYNC, syncNotification(c)) } catch (e: SecurityException) { /* permission revoked */ }
    }

    // ── window countdowns ────────────────────────────────────────────
    /** "Nori may act on this phone", counting down to the minute the window closes by itself. */
    fun interactWindowNotification(c: Context, until: Long): Notification {
        val remaining = Windows.remainingMs(until, System.currentTimeMillis())
        return NotificationCompat.Builder(c, Channels.WINDOWS)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle("Nori may act on this phone")
            .setContentText("The control window closes by itself when the timer ends")
            .setOngoing(true).setOnlyAlertOnce(true)
            .setUsesChronometer(true).setChronometerCountDown(true).setWhen(until).setShowWhen(true)
            .setTimeoutAfter(remaining.coerceAtLeast(1))
            .setContentIntent(openSettings(c, 12))
            .addAction(0, "Close now", control(c, ControlReceiver.CLOSE_INTERACT, 5))
            .build()
    }

    fun showInteractWindow(c: Context, until: Long) {
        if (!canNotify(c)) return
        try { NotificationManagerCompat.from(c).notify(ID_INTERACT, interactWindowNotification(c, until)) } catch (e: SecurityException) { /* permission revoked */ }
    }

    fun cancelInteractWindow(c: Context) { try { NotificationManagerCompat.from(c).cancel(ID_INTERACT) } catch (e: Exception) { /* nothing */ } }

    /** The screen-share window's countdown; it is also the foreground notification of the screen-share service. */
    fun shareNotification(c: Context, until: Long): Notification =
        NotificationCompat.Builder(c, Channels.WINDOWS)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle("Nori may take a screenshot when you ask it something")
            .setContentText("Screen-share window. Android also shows its own capture indicator.")
            .setOngoing(true).setOnlyAlertOnce(true)
            .setUsesChronometer(true).setChronometerCountDown(true).setWhen(until).setShowWhen(true)
            .setContentIntent(openSettings(c, 13))
            .addAction(0, "Stop sharing", control(c, ControlReceiver.STOP_SHARE, 3))
            .build()

    // ── Nori's messages ──────────────────────────────────────────────
    /** One server message as an Android notification. Returns FAILED when it could not be shown, so it is not acknowledged and comes again. */
    fun showMessage(c: Context, slot: Int, m: Message): ShowOutcome {
        if (!canNotify(c)) return ShowOutcome.FAILED
        val channel = Channels.forCategory(m.category)
        val notifId = Channels.notificationId(m.id, slot)
        val body = m.text.ifBlank { m.approval?.summary ?: "" }
        val b = NotificationCompat.Builder(c, channel)
            .setSmallIcon(R.drawable.ic_stat)
            .setContentTitle(Channels.titleFor(m))
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setAutoCancel(true)
            .setContentIntent(openChat(c, notifId, slot))
            .setPriority(if (Channels.importanceOf(channel) >= Channels.IMPORTANCE_HIGH) NotificationCompat.PRIORITY_HIGH else if (Channels.importanceOf(channel) == Channels.IMPORTANCE_DEFAULT) NotificationCompat.PRIORITY_DEFAULT else NotificationCompat.PRIORITY_LOW)
            .setCategory(when (channel) { Channels.REMINDER -> NotificationCompat.CATEGORY_REMINDER; Channels.APPROVAL -> NotificationCompat.CATEGORY_MESSAGE; else -> NotificationCompat.CATEGORY_MESSAGE })
        if (m.ts > 0) b.setWhen(m.ts)
        val ap = m.approval
        if (ap != null && channel == Channels.APPROVAL) {
            if (ap.host.isNotBlank()) b.setSubText(ap.host)
            b.addAction(approvalAction(c, slot, ap.id, true, notifId))
            b.addAction(approvalAction(c, slot, ap.id, false, notifId))
        }
        return try {
            NotificationManagerCompat.from(c).notify(notifId, b.build())
            ShowOutcome.SHOWN
        } catch (e: SecurityException) { ShowOutcome.FAILED }
    }

    /**
     * Approve / Deny. On Android 12+ the action itself requires the device to be unlocked (setAuthenticationRequired), so nobody holding a locked phone can approve.
     * On Android 10-11 that flag does not exist, so the buttons open a small confirmation screen instead, and Android asks for the unlock before it appears.
     */
    private fun approvalAction(c: Context, slot: Int, approvalId: Long, approve: Boolean, notifId: Int): NotificationCompat.Action {
        val label = if (approve) "Approve" else "Deny"
        // Two servers number their approvals from 1 each, and PendingIntents that differ only in their extras are the same PendingIntent: the connection is part of the request code.
        val req = Math.floorMod(approvalId, 50_000L).toInt() * 2 + (if (approve) 1 else 0) + 1000 + slot * 200_000
        return if (Build.VERSION.SDK_INT >= 31) {
            val i = Intent(c, ApprovalReceiver::class.java).putExtra(Approvals.EXTRA_ID, approvalId).putExtra(Approvals.EXTRA_APPROVE, approve).putExtra(Approvals.EXTRA_NOTIF, notifId).putExtra(Approvals.EXTRA_CONN, slot)
            val pi = PendingIntent.getBroadcast(c, req, i, IMMUTABLE_UPDATE)
            NotificationCompat.Action.Builder(0, label, pi).setAuthenticationRequired(true).build()
        } else {
            val i = Intent(c, ApprovalActivity::class.java).putExtra(Approvals.EXTRA_ID, approvalId).putExtra(Approvals.EXTRA_APPROVE, approve).putExtra(Approvals.EXTRA_NOTIF, notifId).putExtra(Approvals.EXTRA_CONN, slot)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_HISTORY)
            NotificationCompat.Action.Builder(0, label, PendingIntent.getActivity(c, req, i, IMMUTABLE_UPDATE)).build()
        }
    }

    fun cancelMessage(c: Context, notifId: Int) { try { NotificationManagerCompat.from(c).cancel(notifId) } catch (e: Exception) { /* nothing */ } }

    /** A newer app is on the server; tapping opens the settings where the Update button is. */
    fun showUpdate(c: Context, version: String) {
        if (!canNotify(c)) return
        val n = NotificationCompat.Builder(c, Channels.UPDATES).setSmallIcon(R.drawable.ic_stat).setContentTitle("Nori app $version is ready")
            .setContentText("Tap to update (you have ${BuildConfig.VERSION_NAME})").setAutoCancel(true).setContentIntent(openSettings(c, 6)).build()
        try { NotificationManagerCompat.from(c).notify(ID_UPDATE, n) } catch (e: SecurityException) { /* permission revoked */ }
    }
}
