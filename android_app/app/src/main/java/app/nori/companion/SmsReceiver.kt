package app.nori.companion

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.Telephony

/**
 * A NEW incoming text, as it arrives (RECEIVE_SMS and nothing more: no inbox is read, no text is sent, no contacts or call log are touched). It returns at once unless the owner turned text sharing on
 * for a connection that allows it (core/Capture.kt decides; short-code and alphanumeric senders only when the server also allows them).
 */
class SmsReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        try {
            if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION) return
            val msgs = Telephony.Sms.Intents.getMessagesFromIntent(intent) ?: return
            val app = context.applicationContext
            // the parts of one long text arrive as several messages from the same sender: put them back together
            for ((from, parts) in msgs.filterNotNull().groupBy { it.originatingAddress ?: "" }) {
                val ts = parts.firstOrNull()?.timestampMillis?.takeIf { it > 0 } ?: System.currentTimeMillis()
                CaptureSync.offerSms(app, from, parts.joinToString("") { it.messageBody ?: "" }, ts)
            }
        } catch (e: Exception) { /* never throw out of a receiver */ }
    }
}
