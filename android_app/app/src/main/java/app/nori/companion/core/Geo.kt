package app.nori.companion.core

import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.asin
import kotlin.math.cos
import kotlin.math.min
import kotlin.math.pow
import kotlin.math.sin
import kotlin.math.sqrt

object Geo {
    const val MIN_RADIUS_M = 100
    const val MAX_RADIUS_M = 5000
    const val DEFAULT_RADIUS_M = 150
    const val MAX_GEOFENCES = 60

    fun validLatLon(lat: Double, lon: Double) = !lat.isNaN() && !lon.isNaN() && lat in -90.0..90.0 && lon in -180.0..180.0 && !(lat == 0.0 && lon == 0.0)

    /** Great-circle distance in metres (haversine). */
    fun distanceM(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val r = 6_371_000.0
        val p1 = Math.toRadians(lat1)
        val p2 = Math.toRadians(lat2)
        val dp = Math.toRadians(lat2 - lat1)
        val dl = Math.toRadians(lon2 - lon1)
        val a = sin(dp / 2).pow(2) + cos(p1) * cos(p2) * sin(dl / 2).pow(2)
        return 2 * r * asin(min(1.0, sqrt(a)))
    }

    /** Phones cannot geofence reliably below about 100 m, so smaller radii are widened; absurdly large ones are capped. */
    fun effectiveRadius(radiusM: Int): Int = if (radiusM <= 0) DEFAULT_RADIUS_M else radiusM.coerceIn(MIN_RADIUS_M, MAX_RADIUS_M)

    /** A nearby saved place with the same centre, so "Save here" does not make duplicates. */
    fun nearestWithin(lat: Double, lon: Double, places: List<PlaceDef>, withinM: Double): PlaceDef? =
        places.map { it to distanceM(lat, lon, it.lat, it.lon) }.filter { it.second <= withinM }.minByOrNull { it.second }?.first

    /** The geofences to register: valid places only, capped, radius widened. */
    fun registrable(places: List<PlaceDef>): List<PlaceDef> =
        places.filter { validLatLon(it.lat, it.lon) }.distinctBy { it.id }.take(MAX_GEOFENCES).map { it.copy(radiusM = effectiveRadius(it.radiusM)) }

    /** Changes whenever the set of fences changes, so the app only re-registers when needed. Names are included (they appear in `snapshot.place`). */
    fun signature(places: List<PlaceDef>): String =
        registrable(places).sortedBy { it.id }.joinToString(";") { "${it.id}|${it.name}|${String.format(java.util.Locale.US, "%.5f", it.lat)}|${String.format(java.util.Locale.US, "%.5f", it.lon)}|${it.radiusM}" }

    fun requestId(placeId: Long) = "nori-place-$placeId"
    fun placeIdFromRequest(id: String): Long? = if (id.startsWith("nori-place-")) id.removePrefix("nori-place-").toLongOrNull() else null
}

/** A place enter/exit as sent to the server. Never carries coordinates. */
data class PlaceEvent(val kind: String, val placeId: Long, val ts: Long) {
    fun toJson(): JSONObject = JSONObject().put("kind", kind).put("place_id", placeId).put("ts", ts)
}

/**
 * Turns raw geofence transitions into the few events worth reporting. The phone's geofencing delivers DWELL (inside for a couple of minutes) and EXIT.
 * Hysteresis: a "left" is held for [SETTLE_MS]; if the phone dwells in the place again within that time the exit was GPS jitter at the edge and both vanish.
 * Repeats of the same state are dropped. State is kept as text so it survives the process dying.
 */
class PlaceTracker(inside: Map<Long, Long> = emptyMap(), pendingLeft: Map<Long, Long> = emptyMap()) {
    /** placeId -> when we arrived. A place not in this map is "outside or never seen". */
    private val inside = LinkedHashMap<Long, Long>(inside)
    private val pendingLeft = LinkedHashMap<Long, Long>(pendingLeft)

    /** A transition from the OS. Returns an event to report now (arrivals), or null (left is held; repeats are dropped). */
    @Synchronized fun onTransition(kind: String, placeId: Long, ts: Long): PlaceEvent? {
        when (kind) {
            "arrived" -> {
                if (pendingLeft.remove(placeId) != null) return null          // flapped: the exit never happened
                if (inside.containsKey(placeId)) return null                  // already there
                inside[placeId] = ts
                return PlaceEvent("arrived", placeId, ts)
            }
            "left" -> {
                if (pendingLeft.containsKey(placeId)) return null
                if (!inside.containsKey(placeId)) {
                    // never saw the arrival (app was off): still worth telling the server, but through the same settle delay
                    inside[placeId] = ts
                }
                pendingLeft[placeId] = ts
                return null
            }
        }
        return null
    }

    /** Held exits that have settled. Call regularly. */
    @Synchronized fun flush(now: Long): List<PlaceEvent> {
        val out = ArrayList<PlaceEvent>()
        val it = pendingLeft.entries.iterator()
        while (it.hasNext()) {
            val e = it.next()
            if (now - e.value >= SETTLE_MS) {
                out.add(PlaceEvent("left", e.key, e.value))
                inside.remove(e.key)
                it.remove()
            }
        }
        return out
    }

    /** Forget places that are no longer defined. */
    @Synchronized fun retain(validIds: Set<Long>) {
        inside.keys.retainAll(validIds)
        pendingLeft.keys.retainAll(validIds)
    }

    @Synchronized fun clear() { inside.clear(); pendingLeft.clear() }

    /** The place the phone is in now (most recent arrival that has not left), or null. */
    @Synchronized fun currentPlaceId(): Long? = inside.entries.filter { !pendingLeft.containsKey(it.key) }.maxByOrNull { it.value }?.key

    @Synchronized fun isInside(placeId: Long) = inside.containsKey(placeId) && !pendingLeft.containsKey(placeId)

    @Synchronized fun hasPending() = pendingLeft.isNotEmpty()

    @Synchronized fun serialize(): String = JSONObject()
        .put("inside", JSONObject().also { j -> inside.forEach { (k, v) -> j.put(k.toString(), v) } })
        .put("pending", JSONObject().also { j -> pendingLeft.forEach { (k, v) -> j.put(k.toString(), v) } }).toString()

    companion object {
        const val SETTLE_MS = 150_000L

        fun parse(text: String?): PlaceTracker = try {
            val j = JSONObject(text ?: "{}")
            fun read(key: String): Map<Long, Long> {
                val o = j.optJSONObject(key) ?: return emptyMap()
                val m = LinkedHashMap<Long, Long>()
                for (k in o.keys()) k.toLongOrNull()?.let { m[it] = o.optLong(k) }
                return m
            }
            PlaceTracker(read("inside"), read("pending"))
        } catch (e: Exception) { PlaceTracker() }
    }
}

/** Events waiting to be sent (the phone may be offline when the geofence fires). Deduped, bounded, oldest dropped first. */
class EventOutbox(events: List<PlaceEvent> = emptyList()) {
    private val items = ArrayList<PlaceEvent>(events)

    @Synchronized fun add(e: PlaceEvent) {
        if (items.any { it == e }) return
        items.add(e)
        while (items.size > MAX_QUEUED) items.removeAt(0)
    }

    @Synchronized fun peek(n: Int = MAX_PER_CALL): List<PlaceEvent> = items.take(min(n, MAX_PER_CALL))
    @Synchronized fun drop(count: Int) { repeat(min(count, items.size)) { items.removeAt(0) } }
    @Synchronized fun size() = items.size

    @Synchronized fun body(): JSONObject = JSONObject().put("events", JSONArray(peek().map { it.toJson() }))

    @Synchronized fun serialize(): String = JSONArray(items.map { it.toJson() }).toString()

    companion object {
        const val MAX_PER_CALL = 50
        const val MAX_QUEUED = 200
        fun parse(text: String?): EventOutbox = try {
            val a = JSONArray(text ?: "[]")
            EventOutbox((0 until a.length()).mapNotNull { a.optJSONObject(it) }.map { PlaceEvent(it.optString("kind"), it.optLong("place_id"), it.optLong("ts")) }
                .filter { it.kind == "arrived" || it.kind == "left" })
        } catch (e: Exception) { EventOutbox() }
    }
}
