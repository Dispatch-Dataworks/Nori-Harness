package app.nori.companion.core

import org.json.JSONObject
import java.net.URLDecoder
import java.net.URLEncoder

/** The server address the owner typed. HTTPS only (the app does not allow cleartext traffic). */
object ServerUrl {
    /** A clean base like `https://nori.example.com` (a path prefix is kept, query/fragment dropped), or null when it cannot be used. */
    fun normalize(input: String?): String? {
        var s = (input ?: "").trim()
        if (s.isEmpty() || s.length > 300 || s.any { it.code < 0x21 || it.code == 0x7f }) return null
        if (!s.contains("://")) s = "https://$s"
        val scheme = s.substringBefore("://").lowercase()
        if (scheme != "https") return null
        val rest = s.substringAfter("://")
        val authority = rest.takeWhile { it != '/' && it != '?' && it != '#' }
        if (authority.isEmpty() || authority.contains('@') || authority.contains('\\')) return null
        val host = authority.substringBefore(':').lowercase()
        if (host.isEmpty() || !host.all { it.isLetterOrDigit() || it == '.' || it == '-' }) return null
        val port = authority.substringAfter(':', "")
        if (authority.contains(':') && (port.toIntOrNull() == null || port.toInt() !in 1..65535)) return null
        val path = rest.drop(authority.length).substringBefore('?').substringBefore('#').trimEnd('/')
        return "https://" + host + (if (port.isNotEmpty() && port != "443") ":$port" else "") + path
    }

    fun host(base: String): String = base.substringAfter("://").substringBefore('/').substringBefore(':').lowercase()

    /** The address of a page on the server. [path] must begin with "/". */
    fun page(base: String, path: String): String = base.trimEnd('/') + (if (path.startsWith("/")) path else "/$path")
}

/** Pairing codes are shown XXXX-XXXX; people type them with spaces, lowercase, or no dash. */
object PairCode {
    fun normalize(raw: String?): String? {
        val c = (raw ?: "").uppercase().filter { it.isLetterOrDigit() }
        if (c.length != 8) return null
        return c.substring(0, 4) + "-" + c.substring(4)
    }
}

/** Everything the app can be asked to do through a link (the pairing link, the widgets, the launcher shortcuts, notifications). */
sealed class Link {
    data class Pair(val code: String, val server: String?) : Link()
    object Settings : Link()
    object Chat : Link()
    object Talk : Link()
    data class Prefill(val text: String) : Link()
    object AddTask : Link()
    object Unknown : Link()
}

object Deeplink {
    const val PAIR_SCHEME = "nori-pair"
    const val APP_SCHEME = "nori-app"

    /** Where the web view goes to prefill the chat box. The server side reads `?prefill=`. */
    fun prefillPath(text: String): String = "/?prefill=" + enc(text.take(400))

    /** Where the web view goes to open the chat ready for voice (push-to-talk). The server side reads `?talk=1`. */
    const val TALK_PATH = "/?talk=1"
    const val CHAT_PATH = "/"

    fun prefillLink(text: String): String = "$APP_SCHEME://prefill?text=" + enc(text.take(400))

    fun enc(s: String): String = URLEncoder.encode(s, "UTF-8").replace("+", "%20")

    private fun dec(s: String): String = try { URLDecoder.decode(s.replace("+", "%2B"), "UTF-8") } catch (e: Exception) { s }

    fun queryOf(link: String): Map<String, String> {
        val q = link.substringAfter('?', "").substringBefore('#')
        if (q.isEmpty()) return emptyMap()
        val m = LinkedHashMap<String, String>()
        for (part in q.split('&')) {
            if (part.isEmpty()) continue
            val k = part.substringBefore('=')
            val v = part.substringAfter('=', "")
            m.putIfAbsent(dec(k), dec(v))
        }
        return m
    }

    fun parse(link: String?): Link {
        val l = (link ?: "").trim()
        val scheme = l.substringBefore("://", "").lowercase()
        if (scheme.isEmpty()) return Link.Unknown
        val after = l.substringAfter("://")
        val host = after.takeWhile { it != '/' && it != '?' && it != '#' }.lowercase()
        val q = queryOf(l)
        return when (scheme) {
            PAIR_SCHEME -> {
                val code = PairCode.normalize(q["code"]) ?: return Link.Unknown
                Link.Pair(code, q["server"]?.let { ServerUrl.normalize(it) })
            }
            APP_SCHEME -> when (host) {
                "settings" -> Link.Settings
                "chat" -> Link.Chat
                "talk" -> Link.Talk
                "task" -> Link.AddTask
                "prefill" -> q["text"]?.takeIf { it.isNotBlank() }?.let { Link.Prefill(it.take(400)) } ?: Link.Chat
                else -> Link.Unknown
            }
            else -> Link.Unknown
        }
    }
}

/** `POST /device/share` bodies: what the Share sheet (and the "Add task" widget) sends. */
object ShareBody {
    const val MAX_TEXT = 8000
    val DESTINATIONS = listOf("chat", "note", "task")

    fun isDest(d: String?) = d in DESTINATIONS

    private val URL_RE = Regex("https?://\\S+")

    /** Text that is essentially one web address is a link; anything else is text. Returns null when there is nothing to send. */
    fun fromText(text: String?, subject: String?, dest: String, note: String? = null): JSONObject? {
        if (!isDest(dest)) return null
        val t = (text ?: "").trim()
        if (t.isEmpty()) return null
        val title = (subject ?: "").trim().take(160)
        val url = URL_RE.find(t)?.value
        val body = JSONObject()
        if (url != null && t.length <= url.length + 140) {
            body.put("kind", "link").put("url", url.take(2000)).put("title", title.ifBlank { t.replace(url, "").trim().take(160) })
        } else {
            body.put("kind", "text").put("text", t.take(MAX_TEXT)).put("title", title)
        }
        body.put("dest", dest)
        note?.trim()?.takeIf { it.isNotEmpty() }?.let { body.put("note", it.take(1000)) }
        return body
    }

    fun image(jpegB64: String, caption: String?, dest: String, note: String? = null): JSONObject? {
        if (!isDest(dest) || jpegB64.isEmpty()) return null
        val body = JSONObject().put("kind", "image").put("jpeg_b64", jpegB64).put("mime", "image/jpeg").put("dest", dest)
        (caption ?: "").trim().takeIf { it.isNotEmpty() }?.let { body.put("text", it.take(MAX_TEXT)) }
        note?.trim()?.takeIf { it.isNotEmpty() }?.let { body.put("note", it.take(1000)) }
        return body
    }
}

/** App updates: when to tell the owner, and which address the APK may be fetched from (the token must never leave the server). */
object UpdateLogic {
    fun parts(v: String): List<Int> = v.trim().split(".").map { it.takeWhile { ch -> ch.isDigit() }.toIntOrNull() ?: 0 }

    fun isNewer(latest: String, mine: String): Boolean {
        if (latest.isBlank()) return false
        val a = parts(latest)
        val b = parts(mine)
        for (i in 0 until maxOf(a.size, b.size)) {
            val x = a.getOrElse(i) { 0 }
            val y = b.getOrElse(i) { 0 }
            if (x != y) return x > y
        }
        return false
    }

    /** Notify once per version: only when it is newer than this build and not already announced. */
    fun shouldNotify(latest: String, mine: String, lastNotified: String): Boolean = isNewer(latest, mine) && latest.trim() != lastNotified.trim()

    /** The APK address on the paired server only. A different host (or anything odd) returns null, so the bearer token is never sent elsewhere. */
    fun resolveApkUrl(base: String, apkUrl: String?): String? {
        val u = (apkUrl ?: "").trim()
        val b = base.trimEnd('/')
        if (u.isEmpty() || u.any { it.code < 0x21 || it.code == 0x7f } || u.contains('\\')) return null
        if (u.startsWith("//")) return null
        if (u.startsWith("/")) return b + u
        if (u.startsWith("https://")) {
            val host = u.substringAfter("://").takeWhile { it != '/' && it != '?' && it != '#' }
            if (host.contains('@')) return null
            return if (host.lowercase() == b.substringAfter("://").substringBefore('/').lowercase()) u else null
        }
        return null
    }
}
