package app.nori.companion

import android.app.Notification
import android.content.ComponentName
import android.content.Context
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import app.nori.companion.core.Capture
import app.nori.companion.core.RawNotification

/**
 * Android's "Notification access" for this app. It is only ever a door: while no connection is set to share notifications, onNotificationPosted returns at once and nothing is read, kept or sent.
 * When one is, the notification is cleaned and filtered by core/Capture.kt (private apps, ongoing and status noise, hidden apps, the connection's level and chosen apps) and queued for that connection only.
 */
class NoriNotificationListener : NotificationListenerService() {

    override fun onListenerConnected() {
        super.onListenerConnected()
        if (Prefs.anyPaired(this)) SyncService.start(this)
    }

    override fun onListenerDisconnected() {
        super.onListenerDisconnected()
        try { requestRebind(ComponentName(this, NoriNotificationListener::class.java)) } catch (e: Exception) { /* the owner may have turned access off */ }
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        try {
            if (sbn == null) return
            val c = applicationContext
            if (!CaptureSync.listenerWanted(c)) return
            CaptureSync.offer(c, build(c, sbn) ?: return)
        } catch (e: Exception) { /* never throw out of a listener */ }
    }

    private fun build(c: Context, sbn: StatusBarNotification): RawNotification? {
        val n = sbn.notification ?: return null
        val pkg = sbn.packageName ?: return null
        val x = n.extras ?: return null
        var text = (x.getCharSequence(Notification.EXTRA_BIG_TEXT) ?: x.getCharSequence(Notification.EXTRA_TEXT))?.toString() ?: ""
        @Suppress("DEPRECATION")
        val messages = x.getParcelableArray(Notification.EXTRA_MESSAGES)
        if (messages != null && messages.isNotEmpty()) {                       // a chat: the last few lines, with who said them
            val list = Notification.MessagingStyle.Message.getMessagesFromBundleArray(messages)
            val tail = Capture.chatTail(list.map { m -> Pair(m.senderPerson?.name?.toString() ?: "", m.text?.toString() ?: "") })
            if (tail.isNotBlank()) text = tail
        }
        val lines = x.getCharSequenceArray(Notification.EXTRA_TEXT_LINES)
        if (lines != null && lines.isNotEmpty() && text.isEmpty()) text = lines.joinToString(" / ") { it.toString() }
        return RawNotification(
            pkg = pkg,
            app = CaptureSync.labelFor(c, pkg),
            title = x.getCharSequence(Notification.EXTRA_TITLE)?.toString() ?: "",
            text = text,
            sub = x.getCharSequence(Notification.EXTRA_SUB_TEXT)?.toString() ?: "",
            category = n.category ?: "",
            key = sbn.key ?: "",
            ts = sbn.postTime,
            ongoing = n.flags and Notification.FLAG_ONGOING_EVENT != 0,
            groupSummary = n.flags and Notification.FLAG_GROUP_SUMMARY != 0,
            secret = n.visibility == Notification.VISIBILITY_SECRET,
        )
    }
}
