package app.nori.companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.core.app.RemoteInput
import app.nori.companion.core.Protocol

/**
 * The two actions on the assistant's message notifications (2026-11-10). REPLY takes the text typed into the notification, or dictated to Android Auto, and sends it to that connection's
 * server as the owner's next chat message (/device/reply, marked "car" while Android Auto is connected). READ clears the notification. Plain text only: the server never treats it as a command.
 */
class ReplyReceiver : BroadcastReceiver() {
    override fun onReceive(c: Context, i: Intent) {
        val slot = i.getIntExtra(EXTRA_SLOT, 0)
        val nid = i.getIntExtra(EXTRA_NOTIF, -1)
        when (i.action) {
            READ -> Notifier.markRead(c, slot, nid)
            REPLY -> {
                val text = RemoteInput.getResultsFromIntent(i)?.getCharSequence(Notifier.KEY_REPLY)?.toString()?.trim().orEmpty()
                if (text.isEmpty()) return
                val pending = goAsync()
                val viaCar = CarWatcher.connected
                Thread {
                    try {
                        val r = if (Prefs.paused(c)) null else Api.post(c, slot, "/device/reply", Protocol.reply(text, viaCar), 20_000)
                        val err = when { r == null -> "the app is paused on this phone"; r.ok -> null; else -> r.error ?: "could not reach the server" }
                        Notifier.showMine(c, slot, nid, text, err)
                        SentLog.add(c, if (err == null) "sent a reply" + (if (viaCar) " from the car" else "") + " to ${Prefs.agent(c, slot)}" else "a reply was not sent: $err")
                    } finally { pending.finish() }
                }.start()
            }
        }
    }

    companion object {
        const val REPLY = "app.nori.companion.REPLY"
        const val READ = "app.nori.companion.MARK_READ"
        const val EXTRA_SLOT = "slot"
        const val EXTRA_NOTIF = "notif"
    }
}
