package app.nori.companion

import android.content.Context
import android.os.Handler
import android.os.Looper
import app.nori.companion.core.PairCode
import app.nori.companion.core.ServerUrl

/** Pairing this phone with the server using the one-time code from Settings -> Devices (the link on that page opens this app with the code and address filled in). */
object PairHelper {
    fun pair(c: Context, serverInput: String, codeInput: String, done: (ok: Boolean, message: String) -> Unit) {
        val app = c.applicationContext
        val main = Handler(Looper.getMainLooper())
        val server = ServerUrl.normalize(serverInput)
        val code = PairCode.normalize(codeInput)
        if (server == null) { done(false, "That server address does not look right. It must start with https://"); return }
        if (code == null) { done(false, "The pairing code is 8 letters or digits, shown as XXXX-XXXX."); return }
        Thread {
            val r = Api.register(app, server, code)
            val token = r.json?.optString("token") ?: ""
            if (r.ok && token.isNotEmpty()) {
                val name = r.json?.optString("name") ?: ""
                Prefs.setPaired(app, server, token, name.ifEmpty { "this phone" })
                SentLog.add(app, "paired with ${ServerUrl.host(server)} as ${name.ifEmpty { "this phone" }}")
                SyncService.start(app)
                main.post { done(true, "Paired as ${name.ifEmpty { "this phone" }}") }
            } else {
                val why = when {
                    r.code == 429 -> "Too many tries. Wait a few minutes and try again."
                    r.code == 401 || r.code == 400 || r.code == 403 || r.code == 404 -> r.json?.optString("error")?.takeIf { it.isNotEmpty() } ?: "That code was not accepted. Make a new one in Settings -> Devices."
                    r.ok -> "The server did not send a token."
                    else -> r.error ?: "pairing failed"
                }
                SentLog.add(app, "pairing failed: $why")
                main.post { done(false, why) }
            }
        }.start()
    }
}
