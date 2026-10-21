package app.nori.companion.core

import org.json.JSONArray
import org.json.JSONObject

object HealthMetrics {
    const val WEIGHT = "weight"
    const val BP = "bp"
    const val STEPS = "steps"
    const val SLEEP = "sleep"
    const val RESTING_HR = "resting_hr"
    val ALL: List<String> = listOf(WEIGHT, BP, STEPS, SLEEP, RESTING_HR)
    val POINT_TYPES: Set<String> = setOf(WEIGHT, BP)
    val DAILY_TYPES: Set<String> = setOf(STEPS, SLEEP, RESTING_HR)

    const val KG_TO_LB = 2.2046226218

    fun label(m: String) = when (m) {
        WEIGHT -> "Weight"; BP -> "Blood pressure"; STEPS -> "Steps"; SLEEP -> "Sleep"; RESTING_HR -> "Resting heart rate"; else -> m
    }

    /** A metric is read only when the server allows health AND names the metric AND the owner turned health on AND turned that metric on. */
    fun enabled(serverHealth: Boolean, serverMetrics: Collection<String>, ownerHealth: Boolean, ownerMetrics: Collection<String>): Set<String> {
        if (!serverHealth || !ownerHealth) return emptySet()
        val s = serverMetrics.map { it.lowercase() }.toSet()
        val o = ownerMetrics.map { it.lowercase() }.toSet()
        return ALL.filter { it in s && it in o }.toSet()
    }

    fun kgToLb(kg: Double): Double = Math.round(kg * KG_TO_LB * 10.0) / 10.0
}

/** One reading, ready to send. Point readings carry `ts` + `id`; daily ones carry `day` (yyyy-MM-dd, the phone's local date). */
data class HealthSample(
    val type: String,
    val value: Double? = null,
    val unit: String? = null,
    val ts: Long? = null,
    val id: String? = null,
    val systolic: Int? = null,
    val diastolic: Int? = null,
    val minutes: Int? = null,
    val day: String? = null,
) {
    /** The key that makes the same reading recognisable next time. */
    fun dedupeKey(): String = when (type) {
        in HealthMetrics.POINT_TYPES -> "$type|" + (id ?: "ts$ts")
        else -> "$type|$day"
    }

    /** What the value looks like, so a daily total that grew (steps) is sent again but an identical one is not. */
    fun signature(): String = when (type) {
        HealthMetrics.BP -> "$systolic/$diastolic"
        HealthMetrics.SLEEP -> "$minutes"
        else -> value?.let { if (it == Math.floor(it)) it.toLong().toString() else it.toString() } ?: ""
    }

    fun toJson(): JSONObject {
        val j = JSONObject().put("type", type)
        when (type) {
            HealthMetrics.WEIGHT -> { j.put("value", value); j.put("unit", unit ?: "lb"); j.put("ts", ts); id?.let { j.put("id", it) } }
            HealthMetrics.BP -> { j.put("systolic", systolic); j.put("diastolic", diastolic); j.put("ts", ts); id?.let { j.put("id", it) } }
            HealthMetrics.STEPS, HealthMetrics.RESTING_HR -> { j.put("value", value?.let { if (it == Math.floor(it)) it.toLong() else it }); j.put("day", day) }
            HealthMetrics.SLEEP -> { j.put("minutes", minutes); j.put("day", day) }
        }
        return j
    }
}

/**
 * Remembers what has already been sent so nothing goes twice, even across restarts and overlapping reads.
 * Point readings (weight, blood pressure) are remembered by id (or timestamp); daily totals by day + value.
 */
class HealthLedger(sentPoints: Collection<String> = emptyList(), daily: Map<String, String> = emptyMap(), lastTs: Map<String, Long> = emptyMap()) {
    private val points = LinkedHashSet<String>(sentPoints)
    private val daily = LinkedHashMap<String, String>(daily)
    private val lastTs = LinkedHashMap<String, Long>(lastTs)

    /** Where to start reading for [type]: a first run looks back [FIRST_LOOKBACK_DAYS]; later runs overlap the last reading by a day (the ids make that safe). */
    @Synchronized fun sinceMs(type: String, now: Long): Long {
        val last = lastTs[type] ?: return now - FIRST_LOOKBACK_DAYS * DAY_MS
        return maxOf(last - DAY_MS, now - FIRST_LOOKBACK_DAYS * DAY_MS)
    }

    /** The samples not sent before, duplicates within [samples] removed. Does not change the ledger. */
    @Synchronized fun filterNew(samples: List<HealthSample>): List<HealthSample> {
        val seen = HashSet<String>()
        val out = ArrayList<HealthSample>()
        for (s in samples) {
            if (s.type !in HealthMetrics.ALL) continue
            val key = s.dedupeKey()
            if (!seen.add(key + "=" + s.signature())) continue
            val isNew = if (s.type in HealthMetrics.POINT_TYPES) key !in points else daily[key] != s.signature()
            if (isNew) out.add(s)
        }
        return out
    }

    /** Call only after the server accepted them. */
    @Synchronized fun markSent(samples: List<HealthSample>) {
        for (s in samples) {
            val key = s.dedupeKey()
            if (s.type in HealthMetrics.POINT_TYPES) {
                points.add(key)
                s.ts?.let { t -> lastTs[s.type] = maxOf(lastTs[s.type] ?: 0L, t) }
            } else {
                daily.remove(key)
                daily[key] = s.signature()
            }
        }
        while (points.size > MAX_POINTS) points.remove(points.first())
        while (daily.size > MAX_DAILY) daily.remove(daily.keys.first())
    }

    @Synchronized fun serialize(): String = JSONObject()
        .put("points", JSONArray(points.toList()))
        .put("daily", JSONObject().also { j -> daily.forEach { (k, v) -> j.put(k, v) } })
        .put("last", JSONObject().also { j -> lastTs.forEach { (k, v) -> j.put(k, v) } }).toString()

    companion object {
        const val FIRST_LOOKBACK_DAYS = 14L
        const val DAY_MS = 86_400_000L
        const val MAX_POINTS = 500
        const val MAX_DAILY = 120
        const val MAX_PER_POST = 200

        fun parse(text: String?): HealthLedger = try {
            val j = JSONObject(text ?: "{}")
            val pts = j.optJSONArray("points")?.let { a -> (0 until a.length()).map { a.getString(it) } } ?: emptyList()
            val d = LinkedHashMap<String, String>()
            j.optJSONObject("daily")?.let { o -> for (k in o.keys()) d[k] = o.optString(k) }
            val l = LinkedHashMap<String, Long>()
            j.optJSONObject("last")?.let { o -> for (k in o.keys()) l[k] = o.optLong(k) }
            HealthLedger(pts, d, l)
        } catch (e: Exception) { HealthLedger() }

        fun body(samples: List<HealthSample>): JSONObject = JSONObject().put("samples", JSONArray(samples.take(MAX_PER_POST).map { it.toJson() }))
    }
}
