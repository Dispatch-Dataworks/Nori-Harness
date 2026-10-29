package app.nori.companion

import android.content.Context
import android.os.Build
import app.nori.companion.core.Connections
import app.nori.companion.core.PollAction
import app.nori.companion.core.PollPolicy
import app.nori.companion.core.Protocol
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.ConcurrentHashMap

/** The phone's side of Nori's /device endpoints (see docs/devices.md). Plain HttpURLConnection and org.json: nothing to download, nothing to go stale. */
object Api {
    class Result(val code: Int, val json: JSONObject?, val text: String, val error: String?) {
        val ok get() = code in 200..299
        val action: PollAction get() = PollPolicy.classify(code)
    }

    /** One long poll per connection can be waiting at a time. */
    private val pollConns = ConcurrentHashMap<Int, HttpURLConnection>()
    private val cancelled = ConcurrentHashMap.newKeySet<Int>()

    /** True once if the last poll of [slot] ended because [cancelPoll] hung up on it (not because the network failed). */
    fun wasCancelled(slot: Int): Boolean = cancelled.remove(slot)

    private fun userAgent() = "NoriCompanion/${BuildConfig.VERSION_NAME} Android/${Build.VERSION.RELEASE}"

    private fun readable(code: Int, json: JSONObject?): String? {
        if (code in 200..299) return null
        val msg = json?.optString("error")?.takeIf { it.isNotEmpty() }
        return when (code) {
            401 -> "this phone is not (or no longer) paired"
            403 -> msg ?: "switched off on the server"
            429 -> msg ?: "too many requests: slow down"
            else -> msg ?: "HTTP $code"
        }
    }

    /**
     * POST JSON to /device/... on the server of connection [slot], with that connection's device token. [server] and [token] override the stored ones (used by pairing,
     * which has no token yet: it passes [Connections.NONE] as the slot and both overrides).
     */
    fun post(c: Context, slot: Int, path: String, body: JSONObject, timeoutMs: Int = 20000, token: String? = null, server: String? = null, isPoll: Boolean = false): Result {
        val base = server ?: Prefs.server(c, slot)
        val tok = token ?: Prefs.token(c, slot)
        if (base.isEmpty()) return Result(0, null, "", "no server address")
        var conn: HttpURLConnection? = null
        return try {
            conn = (URL(base + path).openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                connectTimeout = 15000
                readTimeout = timeoutMs
                doOutput = true
                instanceFollowRedirects = false
                setRequestProperty("Content-Type", "application/json")
                setRequestProperty("Accept", "application/json")
                setRequestProperty("User-Agent", userAgent())
                if (tok.isNotEmpty()) setRequestProperty("Authorization", "Bearer $tok")
            }
            if (isPoll) { cancelled.remove(slot); pollConns[slot] = conn }
            conn.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            val code = conn.responseCode
            val stream = if (code in 200..299) conn.inputStream else conn.errorStream
            val text = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() } ?: ""
            val json = try { JSONObject(text) } catch (e: Exception) { null }
            Result(code, json, text, readable(code, json))
        } catch (e: Exception) {
            Result(0, null, "", e.javaClass.simpleName + ": " + (e.message ?: "").take(120))
        } finally {
            if (isPoll) conn?.let { pollConns.remove(slot, it) }
            try { conn?.disconnect() } catch (e: Exception) { /* closing */ }
        }
    }

    /** Cancel the long poll of every connection that is waiting. Safe: a server only advances its cursor on `ack`, so anything not yet acknowledged comes again. */
    fun cancelPoll() {
        for (slot in pollConns.keys.toList()) cancelPoll(slot)
    }

    fun cancelPoll(slot: Int) {
        val c = pollConns[slot] ?: return
        cancelled.add(slot)
        try { c.disconnect() } catch (e: Exception) { /* already gone */ }
    }

    fun register(c: Context, server: String, code: String): Result =
        post(c, Connections.NONE, "/device/register", Protocol.register(code, Build.MODEL ?: "Android phone", Build.MODEL ?: "", Build.VERSION.RELEASE, BuildConfig.VERSION_NAME), 20000, token = "", server = server)

    /** Download the update APK from connection [slot] with its device token (the caller resolves the address so it is always on that paired server). Returns an error text or null. */
    fun download(c: Context, slot: Int, url: String, dest: File, maxBytes: Long = 200L * 1024 * 1024): String? {
        var conn: HttpURLConnection? = null
        return try {
            conn = (URL(url).openConnection() as HttpURLConnection).apply {
                connectTimeout = 15000; readTimeout = 60000; instanceFollowRedirects = false
                setRequestProperty("Authorization", "Bearer " + Prefs.token(c, slot))
                setRequestProperty("User-Agent", userAgent())
            }
            val code = conn.responseCode
            if (code != 200) return if (code == 401) "this phone is not paired any more" else "the server answered HTTP $code"
            dest.parentFile?.mkdirs()
            var total = 0L
            conn.inputStream.use { input ->
                dest.outputStream().use { out ->
                    val buf = ByteArray(64 * 1024)
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        total += n
                        if (total > maxBytes) { dest.delete(); return "the file is too large" }
                        out.write(buf, 0, n)
                    }
                }
            }
            if (total < 100_000) { dest.delete(); "the download is too small to be the app" } else null
        } catch (e: Exception) {
            try { dest.delete() } catch (x: Exception) { /* nothing */ }
            e.javaClass.simpleName + ": " + (e.message ?: "").take(100)
        } finally {
            try { conn?.disconnect() } catch (e: Exception) { /* closing */ }
        }
    }
}
