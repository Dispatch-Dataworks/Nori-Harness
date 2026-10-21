package app.nori.companion.core

/**
 * `open_url` may only open a public http(s) page. Everything else (other schemes, a private or loopback address, credentials in the address,
 * odd number notations for an IP) is refused here, before any intent is built. No Android classes, no DNS lookups.
 *
 * Known limit: a hostname that merely RESOLVES to a private address (DNS rebinding, "127.0.0.1.nip.io") cannot be seen without resolving it;
 * the common wildcard-DNS services are refused by name.
 */
object UrlGuard {
    class Verdict(val ok: Boolean, val reason: String, val url: String = "")

    private val WILDCARD_DNS = listOf("nip.io", "sslip.io", "xip.io", "localtest.me", "lvh.me", "traefik.me", "localhost.run")
    private val PRIVATE_SUFFIXES = listOf(".localhost", ".local", ".internal", ".lan", ".home", ".home.arpa", ".intranet", ".corp", ".private", ".localdomain", ".invalid", ".test", ".example")
    private val HOST_RE = Regex("^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")

    fun check(raw: String?): Verdict {
        val url = (raw ?: "").trim()
        if (url.isEmpty()) return Verdict(false, "no address was given")
        if (url.length > 2000) return Verdict(false, "that address is too long")
        if (url.any { it.code < 0x21 || it.code == 0x7f }) return Verdict(false, "the address contains spaces or control characters")
        if (url.contains('\\')) return Verdict(false, "the address contains a backslash")
        val schemeEnd = url.indexOf("://")
        if (schemeEnd < 1) return Verdict(false, "only http and https web addresses can be opened")
        val scheme = url.substring(0, schemeEnd).lowercase()
        if (scheme != "http" && scheme != "https") return Verdict(false, "only http and https web addresses can be opened")
        val rest = url.substring(schemeEnd + 3)
        val authority = rest.takeWhile { it != '/' && it != '?' && it != '#' }
        if (authority.isEmpty()) return Verdict(false, "the address has no host")
        if (authority.contains('@')) return Verdict(false, "addresses with a user name or password in them are refused")
        if (authority.startsWith("[")) return Verdict(false, "IPv6 addresses are refused; use a hostname")
        val hostPort = authority.split(':')
        if (hostPort.size > 2) return Verdict(false, "the address is malformed")
        val host = hostPort[0].lowercase().trimEnd('.')
        if (hostPort.size == 2) {
            val port = hostPort[1].toIntOrNull()
            if (port == null || port !in 1..65535) return Verdict(false, "the port in the address is not valid")
        }
        if (host.isEmpty()) return Verdict(false, "the address has no host")
        if (looksNumeric(host)) {
            val v4 = parseIpv4Loose(host) ?: return Verdict(false, "that numeric address is not a normal public address")
            if (!isPublicIpv4(v4)) return Verdict(false, "that address is on a private or local network")
            return Verdict(true, "", url)
        }
        if (!HOST_RE.matches(host)) return Verdict(false, "the host name is not valid")
        if (host == "localhost" || !host.contains('.')) return Verdict(false, "that address is on a private or local network")
        if (PRIVATE_SUFFIXES.any { host.endsWith(it) }) return Verdict(false, "that address is on a private or local network")
        if (WILDCARD_DNS.any { host == it || host.endsWith(".$it") }) return Verdict(false, "that kind of address can point at a private network")
        // the last label of a real public name is never all digits (stops "1.2.3.4.5" style tricks)
        if (host.substringAfterLast('.').all { it.isDigit() }) return Verdict(false, "that numeric address is not a normal public address")
        return Verdict(true, "", url)
    }

    fun isPublicHttp(raw: String?): Boolean = check(raw).ok

    private fun looksNumeric(host: String): Boolean {
        if (host.isEmpty()) return false
        // 127.0.0.1, 2130706433, 0x7f.1, 0177.0.0.1, 127.1: every label is a number in some base
        return host.split('.').all { l -> l.isNotEmpty() && (l.all { it.isDigit() } || (l.startsWith("0x") && l.length > 2 && l.drop(2).all { it.isDigit() || it in 'a'..'f' })) }
    }

    private fun parseLabel(l: String): Long? = try {
        when {
            l.startsWith("0x") -> l.drop(2).toLong(16)
            l.length > 1 && l.startsWith("0") -> l.toLong(8)
            else -> l.toLong(10)
        }
    } catch (e: NumberFormatException) { null }

    /** inet_aton-style parsing (1 to 4 parts, any base). Returns the address as a 32-bit value, or null when it is not a valid address. */
    fun parseIpv4Loose(host: String): Long? {
        val parts = host.split('.')
        if (parts.isEmpty() || parts.size > 4) return null
        val nums = parts.map { parseLabel(it) ?: return null }
        var value = 0L
        for (i in 0 until nums.size - 1) {
            if (nums[i] !in 0..255) return null
            value = value or (nums[i] shl (8 * (3 - i)))
        }
        val lastBits = 8 * (4 - (nums.size - 1))
        val last = nums.last()
        if (last < 0 || last >= (1L shl lastBits)) return null
        return value or last
    }

    fun isPublicIpv4(a: Long): Boolean {
        val b1 = ((a shr 24) and 255).toInt()
        val b2 = ((a shr 16) and 255).toInt()
        if (b1 == 0 || b1 == 10 || b1 == 127) return false
        if (b1 == 100 && b2 in 64..127) return false          // carrier-grade NAT
        if (b1 == 169 && b2 == 254) return false              // link-local, cloud metadata
        if (b1 == 172 && b2 in 16..31) return false
        if (b1 == 192 && b2 == 168) return false
        if (b1 == 192 && b2 == 0) return false
        if (b1 == 198 && (b2 == 18 || b2 == 19)) return false
        if (b1 >= 224) return false                            // multicast, reserved, broadcast
        return true
    }
}
