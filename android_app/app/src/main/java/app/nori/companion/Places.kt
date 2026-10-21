package app.nori.companion

import android.Manifest
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import app.nori.companion.core.EventOutbox
import app.nori.companion.core.Geo
import app.nori.companion.core.PlaceEvent
import app.nori.companion.core.PlaceTracker
import com.google.android.gms.location.Geofence
import com.google.android.gms.location.GeofencingClient
import com.google.android.gms.location.GeofencingEvent
import com.google.android.gms.location.GeofencingRequest
import com.google.android.gms.location.LocationServices

/** The persisted tracker and outbox (the receiver and the service both touch them, so every access goes through here). */
object PlaceState {
    private const val TRACKER = "place_tracker"
    private const val OUTBOX = "event_outbox"

    @Synchronized
    fun transition(c: Context, kind: String, placeId: Long, ts: Long) {
        val t = PlaceTracker.parse(Prefs.get(c, TRACKER))
        val e = t.onTransition(kind, placeId, ts)
        if (e != null) enqueue(c, e)
        Prefs.put(c, TRACKER, t.serialize())
        updateCurrent(c, t)
    }

    /** Held exits that have settled become events. */
    @Synchronized
    fun flush(c: Context, now: Long) {
        val t = PlaceTracker.parse(Prefs.get(c, TRACKER))
        val evs = t.flush(now)
        if (evs.isEmpty()) return
        evs.forEach { enqueue(c, it) }
        Prefs.put(c, TRACKER, t.serialize())
        updateCurrent(c, t)
    }

    @Synchronized
    fun retain(c: Context, ids: Set<Long>) {
        val t = PlaceTracker.parse(Prefs.get(c, TRACKER))
        t.retain(ids)
        Prefs.put(c, TRACKER, t.serialize())
        updateCurrent(c, t)
    }

    @Synchronized
    fun clear(c: Context) {
        Prefs.put(c, TRACKER, "")
        Prefs.put(c, OUTBOX, "")
        Prefs.setCurrentPlace(c, "")
    }

    private fun updateCurrent(c: Context, t: PlaceTracker) {
        val id = t.currentPlaceId()
        val name = if (id == null) "" else Prefs.serverSettings(c).placesList.firstOrNull { it.id == id }?.name ?: ""
        Prefs.setCurrentPlace(c, name)
    }

    private fun enqueue(c: Context, e: PlaceEvent) {
        val o = EventOutbox.parse(Prefs.get(c, OUTBOX))
        o.add(e)
        Prefs.put(c, OUTBOX, o.serialize())
    }

    @Synchronized fun outbox(c: Context): EventOutbox = EventOutbox.parse(Prefs.get(c, OUTBOX))

    @Synchronized
    fun dropSent(c: Context, count: Int) {
        val o = EventOutbox.parse(Prefs.get(c, OUTBOX))
        o.drop(count)
        Prefs.put(c, OUTBOX, o.serialize())
    }

    @Synchronized fun hasPending(c: Context): Boolean = PlaceTracker.parse(Prefs.get(c, TRACKER)).hasPending()
}

/**
 * On-device geofencing for the places the owner defined on the server. The phone itself decides when it arrives or leaves; only "arrived" / "left" with the place id
 * ever leave it (see EventOutbox). No coordinates are sent here, and there is no location trail: the fences are evaluated by Google Play services, not by this app.
 * Needs the owner's switch AND the server's, location permission, and "Allow all the time" (Android's rule for fences that work with the app closed).
 */
object PlaceManager {
    @Volatile private var registeredSignature: String? = null

    fun hasFineLocation(c: Context) = c.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
    fun hasBackgroundLocation(c: Context) = Build.VERSION.SDK_INT < 29 || c.checkSelfPermission(Manifest.permission.ACCESS_BACKGROUND_LOCATION) == PackageManager.PERMISSION_GRANTED
    fun ready(c: Context) = hasFineLocation(c) && hasBackgroundLocation(c)

    fun wanted(c: Context): Boolean = Prefs.active(c) && Prefs.ownerPlaces(c) && Prefs.serverSettings(c).places

    /** Forget what was registered (the service calls this at start, so a reboot or an update registers the fences again). */
    fun forget() { registeredSignature = null }

    private fun pendingIntent(c: Context): PendingIntent =
        PendingIntent.getBroadcast(c.applicationContext, 31, Intent(c.applicationContext, GeofenceReceiver::class.java).setAction("app.nori.companion.GEOFENCE"),
            PendingIntent.FLAG_UPDATE_CURRENT or (if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0))

    /** Registers the right fences for the current server list and switches, or removes them all. Safe to call as often as you like. */
    @Synchronized
    fun sync(c: Context) {
        val app = c.applicationContext
        val client: GeofencingClient = try { LocationServices.getGeofencingClient(app) } catch (e: Exception) { return }
        val pi = pendingIntent(app)
        if (!wanted(app) || !hasFineLocation(app)) {
            if (registeredSignature != "") {
                try { client.removeGeofences(pi) } catch (e: Exception) { /* none */ }
                registeredSignature = ""
                PlaceState.clear(app)
            }
            return
        }
        val settings = Prefs.serverSettings(app)
        val places = Geo.registrable(settings.placesList)
        val sig = Geo.signature(settings.placesList)
        if (registeredSignature == sig) return
        PlaceState.retain(app, places.map { it.id }.toSet())
        try { client.removeGeofences(pi) } catch (e: Exception) { /* none */ }
        if (places.isEmpty()) { registeredSignature = sig; return }
        val fences = places.map {
            Geofence.Builder().setRequestId(Geo.requestId(it.id)).setCircularRegion(it.lat, it.lon, it.radiusM.toFloat()).setExpirationDuration(Geofence.NEVER_EXPIRE)
                .setTransitionTypes(Geofence.GEOFENCE_TRANSITION_DWELL or Geofence.GEOFENCE_TRANSITION_EXIT).setLoiteringDelay(120_000).build()
        }
        val request = GeofencingRequest.Builder().setInitialTrigger(GeofencingRequest.INITIAL_TRIGGER_DWELL).addGeofences(fences).build()
        try {
            client.addGeofences(request, pi)
                .addOnSuccessListener { registeredSignature = sig; SentLog.add(app, "watching ${fences.size} place${if (fences.size == 1) "" else "s"} on this phone (no location is sent)") }
                .addOnFailureListener { SentLog.add(app, "could not watch places: ${it.javaClass.simpleName}") }
        } catch (e: SecurityException) { SentLog.add(app, "could not watch places: location permission is missing") }
    }
}

/** Receives the geofence transitions from Google Play services and hands them to the tracker (which applies the hysteresis). */
class GeofenceReceiver : BroadcastReceiver() {
    override fun onReceive(c: Context, i: Intent) {
        val e = GeofencingEvent.fromIntent(i) ?: return
        if (e.hasError()) { SentLog.add(c, "place watching reported an error (${e.errorCode})"); return }
        if (!PlaceManager.wanted(c)) return                        // switched off or paused since the fence was set
        val kind = when (e.geofenceTransition) {
            Geofence.GEOFENCE_TRANSITION_DWELL -> "arrived"
            Geofence.GEOFENCE_TRANSITION_EXIT -> "left"
            else -> return
        }
        val now = System.currentTimeMillis()
        for (g in e.triggeringGeofences ?: emptyList()) {
            val id = Geo.placeIdFromRequest(g.requestId) ?: continue
            PlaceState.transition(c, kind, id, now)
        }
        try { SyncService.start(c) } catch (x: Exception) { /* the next periodic run sends it */ }
        SyncService.workNow(c)
    }
}
