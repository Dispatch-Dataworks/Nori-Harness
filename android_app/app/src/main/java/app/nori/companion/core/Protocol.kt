package app.nori.companion.core

import org.json.JSONArray
import org.json.JSONObject

/** One place the owner defined on the server (name, centre, radius). */
data class PlaceDef(val id: Long, val name: String, val lat: Double, val lon: Double, val radiusM: Int, val home: Boolean)

/** What the server says the app must obey (the server half of every two-key capability). Everything but notifications defaults to off. */
data class DeviceSettings(
    val notifications: Boolean = true,
    val health: Boolean = false,
    val places: Boolean = false,
    val actions: Boolean = false,
    val control: Boolean = false,
    val screen: Boolean = false,
    val quiet: Boolean = false,
    val hiddenApps: List<String> = emptyList(),
    val neverControlApps: List<String> = emptyList(),
    val healthMetrics: List<String> = emptyList(),
    val placesList: List<PlaceDef> = emptyList(),
    val actionsPerHour: Int = 30,
) {
    fun toJson(): JSONObject = JSONObject()
        .put("notifications", notifications).put("health", health).put("places", places).put("actions", actions).put("control", control).put("screen", screen)
        .put("quiet", quiet).put("hidden_apps", JSONArray(hiddenApps)).put("never_control_apps", JSONArray(neverControlApps))
        .put("health_metrics", JSONArray(healthMetrics)).put("actions_per_hour", actionsPerHour)
        .put("places_list", JSONArray(placesList.map { JSONObject().put("id", it.id).put("name", it.name).put("lat", it.lat).put("lon", it.lon).put("radius_m", it.radiusM).put("home", it.home) }))

    companion object {
        fun fromJson(j: JSONObject?): DeviceSettings {
            if (j == null) return DeviceSettings()
            fun strings(key: String): List<String> {
                val a = j.optJSONArray(key) ?: return emptyList()
                return (0 until a.length()).mapNotNull { a.optString(it, "").trim().takeIf { s -> s.isNotEmpty() } }.take(500)
            }
            val places = ArrayList<PlaceDef>()
            j.optJSONArray("places_list")?.let { a ->
                for (i in 0 until minOf(a.length(), 200)) {
                    val p = a.optJSONObject(i) ?: continue
                    val id = p.optLong("id", -1)
                    val lat = p.optDouble("lat", Double.NaN)
                    val lon = p.optDouble("lon", Double.NaN)
                    if (id < 0 || !Geo.validLatLon(lat, lon)) continue
                    places.add(PlaceDef(id, p.optString("name", "").trim().take(60).ifEmpty { "Place $id" }, lat, lon, p.optInt("radius_m", 0), p.optBoolean("home", false)))
                }
            }
            return DeviceSettings(
                notifications = j.optBoolean("notifications", true),
                health = j.optBoolean("health", false),
                places = j.optBoolean("places", false),
                actions = j.optBoolean("actions", false),
                control = j.optBoolean("control", false),
                screen = j.optBoolean("screen", false),
                quiet = j.optBoolean("quiet", false),
                hiddenApps = strings("hidden_apps"),
                neverControlApps = strings("never_control_apps"),
                healthMetrics = strings("health_metrics").map { it.lowercase() },
                placesList = places,
                actionsPerHour = j.optInt("actions_per_hour", 30).coerceIn(0, 10_000),
            )
        }

        fun parse(text: String?): DeviceSettings = try { fromJson(JSONObject(text ?: "{}")) } catch (e: Exception) { DeviceSettings() }
    }
}

data class ApprovalReq(val id: Long, val summary: String, val host: String)
data class Message(val id: Long, val category: String, val title: String, val text: String, val ts: Long, val approval: ApprovalReq?)
data class Command(val id: Long, val action: String, val args: JSONObject, val approved: Boolean, val forceApproval: Boolean)
data class AppInfo(val latest: String, val apkUrl: String)

/** `settings` is null when the server did not send one (keep what the app already has). */
data class PollResponse(val active: Boolean, val messages: List<Message>, val command: Command?, val settings: DeviceSettings?, val app: AppInfo?)

/** What the device itself allows, as reported in `caps` (its own half; the server combines it with its half). */
data class Caps(val notifications: Boolean, val share: Boolean, val health: Boolean, val places: Boolean, val actions: Boolean, val screen: Boolean, val control: Boolean) {
    fun toJson(): JSONObject = JSONObject().put("notifications", notifications).put("share", share).put("health", health).put("places", places)
        .put("actions", actions).put("screen", screen).put("control", control)
}

class Snapshot(
    val battery: Int?, val charging: Boolean, val network: String, val screenOn: Boolean, val locked: Boolean, val dnd: Boolean,
    /** Only the NAME of the place the phone is in; never coordinates. Null/empty = not in a known place. */
    val place: String?,
) {
    fun toJson(): JSONObject {
        val j = JSONObject()
        if (battery != null && battery in 0..100) j.put("battery", battery)
        j.put("charging", charging).put("network", network).put("screen_on", screenOn).put("locked", locked).put("dnd", dnd)
        if (!place.isNullOrBlank()) j.put("place", place.take(60))
        return j
    }
}

/** Builds every request body and parses every response, so the wire format is checked by plain JVM tests. */
object Protocol {
    const val MAX_MESSAGES_PER_POLL = 20

    fun pollRequest(version: String, paused: Boolean, appVisible: Boolean, ack: Long, caps: Caps, interactUntil: Long, shareUntil: Long, snapshot: Snapshot): JSONObject =
        JSONObject().put("v", version).put("paused", paused).put("app_visible", appVisible).put("ack", ack).put("caps", caps.toJson())
            .put("interact_until", interactUntil).put("share_until", shareUntil).put("snapshot", snapshot.toJson())

    /** What a paused phone says: a heartbeat and nothing else (no snapshot, no windows). */
    fun pausedHeartbeat(version: String, ack: Long): JSONObject =
        JSONObject().put("v", version).put("paused", true).put("app_visible", false).put("ack", ack)
            .put("caps", Caps(false, false, false, false, false, false, false).toJson()).put("interact_until", 0).put("share_until", 0)

    fun parsePoll(text: String?): PollResponse? {
        val j = try { JSONObject(text ?: return null) } catch (e: Exception) { return null }
        val msgs = ArrayList<Message>()
        val arr = j.optJSONArray("messages")
        if (arr != null) for (i in 0 until arr.length()) {
            val m = arr.optJSONObject(i) ?: continue
            val id = m.optLong("id", -1)
            if (id < 0) continue
            val ap = m.optJSONObject("approval")?.let { a ->
                val aid = a.optLong("id", -1)
                if (aid < 0) null else ApprovalReq(aid, a.optString("summary", "").take(300), a.optString("host", "").take(120))
            }
            msgs.add(Message(id, m.optString("category", "chat").lowercase(), m.optString("title", "").take(80), m.optString("text", "").take(4000), m.optLong("ts", 0), ap))
        }
        val cmd = j.optJSONObject("command")?.let { c ->
            val id = c.optLong("id", -1)
            if (id < 0) null else Command(id, c.optString("action", ""), c.optJSONObject("args") ?: JSONObject(), c.optBoolean("approved", false), c.optBoolean("force_approval", false))
        }
        val settings = j.optJSONObject("settings")?.let { DeviceSettings.fromJson(it) }
        val app = j.optJSONObject("app")?.let { AppInfo(it.optString("latest", ""), it.optString("apk_url", "")) }
        return PollResponse(j.optBoolean("active", true), msgs.sortedBy { it.id }.take(MAX_MESSAGES_PER_POLL), cmd, settings, app)
    }

    fun ack(id: Long): JSONObject = JSONObject().put("id", id)

    fun resultOk(id: Long, result: JSONObject): JSONObject = JSONObject().put("id", id).put("ok", true).put("result", result)

    /** An error is never blank. */
    fun resultError(id: Long, error: String): JSONObject = JSONObject().put("id", id).put("ok", false).put("error", error.trim().ifEmpty { "the phone could not do that" }.take(400))

    /** Nothing was done; the owner is asked first. */
    fun resultNeedsApproval(id: Long, reason: String, describe: String, fingerprint: String): JSONObject =
        JSONObject().put("id", id).put("needs_approval", true).put("reason", reason.take(200))
            .put("element", JSONObject().put("describe", describe.take(160)).put("fingerprint", fingerprint))

    fun approval(id: Long, approve: Boolean): JSONObject = JSONObject().put("id", id).put("decision", if (approve) "approve" else "deny")

    fun register(code: String, name: String, model: String, android: String, version: String): JSONObject =
        JSONObject().put("code", code).put("kind", "phone").put("name", name).put("model", model).put("android", android).put("v", version)

    fun place(name: String, lat: Double, lon: Double, radiusM: Int?, home: Boolean): JSONObject {
        val j = JSONObject().put("name", name.trim().take(60)).put("lat", lat).put("lon", lon).put("home", home)
        if (radiusM != null) j.put("radius_m", radiusM)
        return j
    }
}
