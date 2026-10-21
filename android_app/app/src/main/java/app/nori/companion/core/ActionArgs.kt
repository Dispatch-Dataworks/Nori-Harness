package app.nori.companion.core

import org.json.JSONObject
import java.net.URLEncoder

/** Validated arguments for the intent-based phone actions. A null error means "valid"; the value classes carry clean, clamped data. */
object ActionArgs {
    class Timer(val seconds: Int, val label: String)
    class Alarm(val hour: Int, val minute: Int, val label: String, /** java.util.Calendar day numbers: SUNDAY=1 .. SATURDAY=7 */ val days: List<Int>)
    class Parsed<T>(val value: T?, val error: String?)

    private fun <T> fail(msg: String) = Parsed<T>(null, msg)

    fun cleanLabel(s: String?): String = (s ?: "").filter { it.code >= 0x20 && it.code != 0x7f }.trim().take(60)

    fun timer(args: JSONObject?): Parsed<Timer> {
        val a = args ?: return fail("no arguments")
        if (!a.has("seconds")) return fail("a timer needs seconds")
        val s = a.optDouble("seconds", Double.NaN)
        if (s.isNaN() || s < 1) return fail("a timer needs a positive number of seconds")
        if (s > 24 * 3600) return fail("a timer can be at most 24 hours")
        return Parsed(Timer(s.toInt(), cleanLabel(a.optString("label", ""))), null)
    }

    private val DAY_NAMES = mapOf(
        "sun" to 1, "sunday" to 1, "mon" to 2, "monday" to 2, "tue" to 3, "tues" to 3, "tuesday" to 3, "wed" to 4, "wednesday" to 4,
        "thu" to 5, "thur" to 5, "thurs" to 5, "thursday" to 5, "fri" to 6, "friday" to 6, "sat" to 7, "saturday" to 7,
    )

    /** `days` may be numbers 1..7 (Sunday = 1, as java.util.Calendar) or day names. */
    fun alarm(args: JSONObject?): Parsed<Alarm> {
        val a = args ?: return fail("no arguments")
        if (!a.has("hour") || !a.has("minute")) return fail("an alarm needs hour and minute")
        val h = a.optInt("hour", -1)
        val m = a.optInt("minute", -1)
        if (h !in 0..23) return fail("the hour must be 0 to 23")
        if (m !in 0..59) return fail("the minute must be 0 to 59")
        val days = LinkedHashSet<Int>()
        val arr = a.optJSONArray("days")
        if (arr != null) for (i in 0 until minOf(arr.length(), 14)) {
            val v = arr.opt(i)
            val d = when (v) {
                is Number -> v.toInt()
                is String -> v.trim().lowercase().toIntOrNull() ?: DAY_NAMES[v.trim().lowercase()]
                else -> null
            } ?: return fail("a day in `days` was not understood")
            if (d !in 1..7) return fail("days must be 1 (Sunday) to 7 (Saturday) or day names")
            days.add(d)
        }
        return Parsed(Alarm(h, m, cleanLabel(a.optString("label", "")), days.toList()), null)
    }

    /** The geo: URI for a search in the maps app. */
    fun navigateUri(args: JSONObject?): Parsed<String> {
        val q = (args?.optString("query", "") ?: "").filter { it.code >= 0x20 && it.code != 0x7f }.trim()
        if (q.isEmpty()) return fail("navigate needs a place or address")
        if (q.length > 200) return fail("that place name is too long")
        return Parsed("geo:0,0?q=" + URLEncoder.encode(q, "UTF-8").replace("+", "%20"), null)
    }

    /** open_app takes a package or a name; exactly one must be usable. */
    class AppTarget(val pkg: String?, val name: String?)

    fun appTarget(args: JSONObject?): Parsed<AppTarget> {
        val pkg = (args?.optString("package", "") ?: "").trim()
        val name = (args?.optString("name", "") ?: "").trim()
        if (pkg.isNotEmpty()) {
            if (!AppGuard.isValidPackageName(pkg)) return fail("that is not a valid package name")
            return Parsed(AppTarget(pkg, null), null)
        }
        if (name.isNotEmpty()) return Parsed(AppTarget(null, name.take(80)), null)
        return fail("open_app needs a package or a name")
    }

    /** Best launcher match for a spoken name: exact (case-insensitive) first, then a unique "contains". Ambiguity is an error, not a guess. */
    fun resolveByName(name: String, apps: List<Pair<String, String>> /* package to label */): Parsed<String> {
        val n = name.trim().lowercase()
        val exact = apps.filter { it.second.lowercase() == n }
        if (exact.size == 1) return Parsed(exact[0].first, null)
        if (exact.size > 1) return fail("more than one app is called that")
        val partial = apps.filter { it.second.lowercase().contains(n) }
        if (partial.size == 1) return Parsed(partial[0].first, null)
        if (partial.isEmpty()) return fail("no app with that name was found")
        return fail("more than one app matches that name; use the package")
    }
}
