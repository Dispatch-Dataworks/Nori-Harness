package app.nori.companion

import android.content.Context
import android.os.Handler
import android.os.Looper
import app.nori.companion.core.Connections
import app.nori.companion.core.PairCode
import app.nori.companion.core.ServerUrl

/**
 * Pairing this phone with a server using the one-time code from Settings -> Devices (the link on that page opens this app with the code and address filled in).
 * The phone can hold several pairings at once: a server it already has is paired again in its own slot, a new one takes a free slot (see [Connections.pairTarget]).
 */
object PairHelper {
    /** Where pairing with [serverInput] would go, without doing it (so the confirmation can say "this adds another connection"). Null when the address is not usable. */
    fun target(c: Context, serverInput: String): Connections.Target? {
        val server = ServerUrl.normalize(serverInput) ?: return null
        return Connections.pairTarget(Prefs.slots(c).map { Connections.Saved(it, Prefs.host(c, it), Prefs.isPaired(c, it)) }, ServerUrl.host(server))
    }

    fun pair(c: Context, serverInput: String, codeInput: String, done: (ok: Boolean, message: String, slot: Int) -> Unit) {
        val app = c.applicationContext
        val main = Handler(Looper.getMainLooper())
        val server = ServerUrl.normalize(serverInput)
        val code = PairCode.normalize(codeInput)
        if (server == null) { done(false, "That server address does not look right. It must start with https://", Connections.NONE); return }
        if (code == null) { done(false, "The pairing code is 8 letters or digits, shown as XXXX-XXXX.", Connections.NONE); return }
        val target = target(app, server)!!
        if (target.kind == Connections.Kind.FULL) { done(false, "This phone already has ${Connections.MAX} connections. Remove one in the app settings first.", Connections.NONE); return }
        val slot = target.slot
        Thread {
            val r = Api.register(app, server, code)
            val token = r.json?.optString("token") ?: ""
            if (r.ok && token.isNotEmpty()) {
                val name = r.json?.optString("name") ?: ""
                Prefs.setPaired(app, slot, server, token, name.ifEmpty { "this phone" })
                if (target.kind != Connections.Kind.SAME_SERVER) Prefs.setLabel(app, slot, "")       // a different server in a reused slot must not keep the old name
                SentLog.add(app, "paired with ${ServerUrl.host(server)} as ${name.ifEmpty { "this phone" }}")
                SyncService.start(app)
                SyncService.refresh(app)
                main.post { done(true, "Paired as ${name.ifEmpty { "this phone" }}", slot) }
            } else {
                val why = when {
                    r.code == 429 -> "Too many tries. Wait a few minutes and try again."
                    r.code == 401 || r.code == 400 || r.code == 403 || r.code == 404 -> r.json?.optString("error")?.takeIf { it.isNotEmpty() } ?: "That code was not accepted. Make a new one in Settings -> Devices."
                    r.ok -> "The server did not send a token."
                    else -> r.error ?: "pairing failed"
                }
                SentLog.add(app, "pairing failed: $why")
                main.post { done(false, why, Connections.NONE) }
            }
        }.start()
    }
}
