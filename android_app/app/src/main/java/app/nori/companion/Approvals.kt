package app.nori.companion

import android.app.Activity
import android.app.AlertDialog
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.Toast
import app.nori.companion.core.Protocol

/** Sends the owner's Approve / Deny for an approval notification to `POST /device/approval`. */
object Approvals {
    const val EXTRA_ID = "approval_id"
    const val EXTRA_APPROVE = "approve"
    const val EXTRA_NOTIF = "notif_id"
    const val EXTRA_CONN = "conn"

    /** Blocking: call off the main thread. The decision goes to the server of connection [slot] (the one the approval came from). Returns an error text, or null when the server took it. */
    fun send(c: Context, slot: Int, id: Long, approve: Boolean, notifId: Int): String? {
        if (!Prefs.isPaired(c, slot) || Prefs.needsRepair(c, slot)) return "this phone is not paired"
        if (Prefs.paused(c)) return "Nori is paused on this phone: resume it first"
        val r = Api.post(c, slot, "/device/approval", Protocol.approval(id, approve))
        val who = if (Prefs.slots(c).size > 1) " (${Prefs.name(c, slot)})" else ""
        SentLog.add(c, if (r.ok) "approval #$id$who: ${if (approve) "approved" else "denied"}" else "approval #$id$who could not be sent: ${r.error}")
        if (r.ok) Notifier.cancelMessage(c, notifId)
        return if (r.ok) null else (r.error ?: "could not reach the server")
    }

    fun toast(c: Context, text: String) { Handler(Looper.getMainLooper()).post { Toast.makeText(c.applicationContext, text, Toast.LENGTH_LONG).show() } }
}

/** Android 12+: the notification's Approve / Deny buttons land here, only after the device was unlocked (the action is built with setAuthenticationRequired). */
class ApprovalReceiver : BroadcastReceiver() {
    override fun onReceive(c: Context, i: Intent) {
        val id = i.getLongExtra(Approvals.EXTRA_ID, -1L)
        if (id < 0) return
        val approve = i.getBooleanExtra(Approvals.EXTRA_APPROVE, false)
        val notif = i.getIntExtra(Approvals.EXTRA_NOTIF, 0)
        val slot = i.getIntExtra(Approvals.EXTRA_CONN, 0)
        val pending = goAsync()
        val app = c.applicationContext
        Thread {
            try {
                val err = Approvals.send(app, slot, id, approve, notif)
                Approvals.toast(app, if (err == null) (if (approve) "Approved" else "Denied") else "Not sent: $err")
            } finally { pending.finish() }
        }.start()
    }
}

/**
 * Android 10-11: there is no "unlock required" flag for notification buttons, so Approve / Deny open this confirmation instead. The screen is not allowed
 * over the lock screen, so Android asks for the unlock first; the decision is only sent when the owner taps the dialog button.
 */
class ApprovalActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val id = intent.getLongExtra(Approvals.EXTRA_ID, -1L)
        val approve = intent.getBooleanExtra(Approvals.EXTRA_APPROVE, false)
        val notif = intent.getIntExtra(Approvals.EXTRA_NOTIF, 0)
        val slot = intent.getIntExtra(Approvals.EXTRA_CONN, 0)
        if (id < 0) { finish(); return }
        AlertDialog.Builder(this)
            .setTitle(if (approve) "Approve this?" else "Deny this?")
            .setMessage(if (approve) "Nori will go ahead with the action it asked about." else "Nori will not do it.")
            .setPositiveButton(if (approve) "Approve" else "Deny") { _, _ ->
                val app = applicationContext
                Thread {
                    val err = Approvals.send(app, slot, id, approve, notif)
                    Approvals.toast(app, if (err == null) (if (approve) "Approved" else "Denied") else "Not sent: $err")
                }.start()
                finish()
            }
            .setNegativeButton("Cancel") { _, _ -> finish() }
            .setOnCancelListener { finish() }
            .show()
    }
}
