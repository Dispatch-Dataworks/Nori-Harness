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

/** The persisted tracker and outbox of each connection (the receiver and the service both touch them, so every access goes through here). Place ids are per server, so each connection keeps its own. */
object PlaceState {
    private const val TRACKER = "place_tracker"
    private const val OUTBOX = "event_outbox"

    @Synchronized
    fun transition(c: Context, slot: Int, kind: String, placeId: Long, ts: Long) {
        val t = PlaceTracker.parse(Prefs.getFor(c, slot, TRACKER))
        val e = t.onTransition(kind, placeId, ts)
        if (e != null) enqueue(c, slot, e)
        Prefs.putFor(c, slot, TRACKER, t.serialize())
        updateCurrent(c, slot, t)
    }

    /** Held exits that have settled become events. */
    @Synchronized
    fun flush(c: Context, slot: Int, now: Long) {
        val t = PlaceTracker.parse(Prefs.getFor(c, slot, TRACKER))
        val evs = t.flush(now)
        if (evs.isEmpty()) return
        evs.forEach { enqueue(c, slot, it) }
        Prefs.putFor(c, slot, TRACKER, t.serialize())
        updateCurrent(c, slot, t)
    }

    @Synchronized
    fun retain(c: Context, slot: Int, ids: Set<Long>) {
        val t = PlaceTracker.parse(Prefs.getFor(c, slot, TRACKER))
        t.retain(ids)
        Prefs.putFor(c, slot, TRACKER, t.serialize())
        updateCurrent(c, slot, t)
    }

    @Synchronized
    fun clear(c: Context, slot: Int) {
        Prefs.putFor(c, slot, TRACKER, "")
        Prefs.putFor(c, slot, OUTBOX, "")
        Prefs.setCurrentPlace(c, slot, "")
    }

    private fun updateCurrent(c: Context, slot: Int, t: PlaceTracker) {
        val id = t.currentPlaceId()
        val name = if (id == null) "" else Prefs.serverSettings(c, slot).placesList.firstOrNull { it.id == id }?.name ?: ""
        Prefs.setCurrentPlace(c, slot, name)
    }

    private fun enqueue(c: Context, slot: Int, e: PlaceEvent) {
        val o = EventOutbox.parse(Prefs.getFor(c, slot, OUTBOX))
        o.add(e)
        Prefs.putFor(c, slot, OUTBOX, o.serialize())
    }

    @Synchronized fun outbox(c: Context, slot: Int): EventOutbox = EventOutbox.parse(Prefs.getFor(c, slot, OUTBOX))

    @Synchronized
    fun dropSent(c: Context, slot: Int, count: Int) {
        val o = EventOutbox.parse(Prefs.getFor(c, slot, OUTBOX))
        o.drop(count)
        Prefs.putFor(c, slot, OUTBOX, o.serialize())
    }
}

/**
 * On-device geofencing for the places the owner defined on each server. The phone itself decides when it arrives or leaves; only "arrived" / "left" with the place id
 * ever leave it (see EventOutbox), and each event goes only to the server that owns that place. No coordinates are sent here, and there is no location trail: the fences are
 * evaluated by Google Play services, not by this app. Needs the owner's switch AND the server's, location permission, and "Allow all the time" (Android's rule for fences that work with the app closed).
 */
object PlaceManager {
    @Volatile private var registeredSignature: String? = null

    fun hasFineLocation(c: Context) = c.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
    fun hasBackgroundLocation(c: Context) = Build.VERSION.SDK_INT < 29 || c.checkSelfPermission(Manifest.permission.ACCESS_BACKGROUND_LOCATION) == PackageManager.PERMISSION_GRANTED
    fun ready(c: Context) = hasFineLocation(c) && hasBackgroundLocation(c)

    fun wanted(c: Context, slot: Int): Boolean = Prefs.active(c, slot) && Prefs.ownerPlaces(c) && Prefs.serverSettings(c, slot).places

    /** Forget what was registered (the service calls this at start, so a reboot or an update registers the fences again). */
    fun forget() { registeredSignature = null }

    private fun pendingIntent(c: Context): PendingIntent =
        PendingIntent.getBroadcast(c.applicationContext, 31, Intent(c.applicationContext, GeofenceReceiver::class.java).setAction("app.nori.companion.GEOFENCE"),
            PendingIntent.FLAG_UPDATE_CURRENT or (if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0))

    /** Registers the right fences for every connection's place list and the switches, or removes them all. Safe to call as often as you like. */
    @Synchronized
    fun sync(c: Context) {
        val app = c.applicationContext
        val client: GeofencingClient = try { LocationServices.getGeofencingClient(app) } catch (e: Exception) { return }
        val pi = pendingIntent(app)
        val wanting = Prefs.slots(app).filter { wanted(app, it) }
        if (wanting.isEmpty() || !hasFineLocation(app)) {
            if (registeredSignature != "") {
                try { client.removeGeofences(pi) } catch (e: Exception) { /* none */ }
                registeredSignature = ""
                for (slot in Prefs.slots(app)) PlaceState.clear(app, slot)
            }
            return
        }
        val perSlot = wanting.associateWith { Geo.registrable(Prefs.serverSettings(app, it).placesList) }
        val sig = wanting.joinToString("#") { "$it:" + Geo.signature(Prefs.serverSettings(app, it).placesList) }
        if (registeredSignature == sig) return
        for (slot in Prefs.slots(app)) {
            if (slot in perSlot) PlaceState.retain(app, slot, perSlot.getValue(slot).map { it.id }.toSet()) else PlaceState.clear(app, slot)   // a connection that stopped wanting places forgets them
        }
        try { client.removeGeofences(pi) } catch (e: Exception) { /* none */ }
        val fences = perSlot.flatMap { (slot, places) -> places.map { slot to it } }.take(Geo.MAX_TOTAL_GEOFENCES).map { (slot, p) ->
            Geofence.Builder().setRequestId(Geo.requestId(p.id, slot)).setCircularRegion(p.lat, p.lon, p.radiusM.toFloat()).setExpirationDuration(Geofence.NEVER_EXPIRE)
                .setTransitionTypes(Geofence.GEOFENCE_TRANSITION_DWELL or Geofence.GEOFENCE_TRANSITION_EXIT).setLoiteringDelay(120_000).build()
        }
        if (fences.isEmpty()) { registeredSignature = sig; return }
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
        val kind = when (e.geofenceTransition) {
            Geofence.GEOFENCE_TRANSITION_DWELL -> "arrived"
            Geofence.GEOFENCE_TRANSITION_EXIT -> "left"
            else -> return
        }
        val now = System.currentTimeMillis()
        for (g in e.triggeringGeofences ?: emptyList()) {
            val (slot, id) = Geo.parseRequest(g.requestId) ?: continue
            if (!PlaceManager.wanted(c, slot)) continue                  // switched off, paused or unpaired since the fence was set
            PlaceState.transition(c, slot, kind, id, now)
        }
        try { SyncService.start(c) } catch (x: Exception) { /* the next periodic run sends it */ }
        SyncService.workNow(c)
    }
}
