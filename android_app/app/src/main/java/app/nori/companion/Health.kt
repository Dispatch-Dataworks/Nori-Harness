package app.nori.companion

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.widget.ScrollView
import android.widget.TextView
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.PermissionController
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.BloodPressureRecord
import androidx.health.connect.client.records.RestingHeartRateRecord
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.WeightRecord
import androidx.health.connect.client.request.AggregateRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import app.nori.companion.core.HealthLedger
import app.nori.companion.core.HealthMetrics
import app.nori.companion.core.HealthSample
import app.nori.companion.core.Protocol
import kotlinx.coroutines.runBlocking
import java.time.Duration
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import kotlin.reflect.KClass

/**
 * What Health Connect holds that Nori may read: weight, blood pressure, steps, sleep, resting heart rate. Only the metrics the server allows AND the owner switched on
 * are read; this app only READS, never writes. What was already sent is remembered (HealthLedger), so nothing goes twice.
 */
object HealthReader {
    private val recordFor: Map<String, KClass<out androidx.health.connect.client.records.Record>> = mapOf(
        HealthMetrics.WEIGHT to WeightRecord::class, HealthMetrics.BP to BloodPressureRecord::class, HealthMetrics.STEPS to StepsRecord::class,
        HealthMetrics.SLEEP to SleepSessionRecord::class, HealthMetrics.RESTING_HR to RestingHeartRateRecord::class,
    )
    const val BACKGROUND = "android.permission.health.READ_HEALTH_DATA_IN_BACKGROUND"

    @Volatile var lastError: String = ""

    fun available(c: Context) = HealthConnectClient.getSdkStatus(c) == HealthConnectClient.SDK_AVAILABLE
    private fun client(c: Context) = HealthConnectClient.getOrCreate(c)

    fun permissionFor(metric: String): String? = recordFor[metric]?.let { HealthPermission.getReadPermission(it) }

    fun granted(c: Context): Set<String> = try { if (available(c)) runBlocking { client(c).permissionController.getGrantedPermissions() } else emptySet() } catch (e: Exception) { emptySet() }

    fun grantedMetrics(c: Context): Set<String> { val g = granted(c); return HealthMetrics.ALL.filter { permissionFor(it) in g }.toSet() }

    /** The Android screen that asks the owner which of these to allow. */
    fun requestIntent(c: Context, metrics: Collection<String>): Intent {
        val perms = metrics.mapNotNull { permissionFor(it) }.toMutableSet()
        perms.add(BACKGROUND)
        return PermissionController.createRequestPermissionResultContract().createIntent(c, perms)
    }

    private fun dayOf(t: Instant, zone: ZoneId): String = t.atZone(zone).toLocalDate().toString()

    /** New readings for [metrics] (already filtered against [ledger]). Never throws: a failure of one metric is noted in [lastError] and the others carry on. */
    fun read(c: Context, metrics: Set<String>, ledger: HealthLedger, nowMs: Long = System.currentTimeMillis()): List<HealthSample> {
        if (metrics.isEmpty()) return emptyList()
        if (!available(c)) { lastError = "Health Connect is not available on this phone"; return emptyList() }
        val grant = granted(c)
        val errors = ArrayList<String>()
        val out = ArrayList<HealthSample>()
        val hc = client(c)
        val now = Instant.ofEpochMilli(nowMs)
        val zone = ZoneId.systemDefault()
        val startOfToday = now.atZone(zone).toLocalDate().atStartOfDay(zone).toInstant()

        fun part(metric: String, block: suspend () -> Unit) {
            if (metric !in metrics) return
            val perm = permissionFor(metric)
            if (perm == null || perm !in grant) { errors.add("${HealthMetrics.label(metric)}: not allowed in Health Connect yet"); return }
            try { runBlocking { block() } } catch (e: Exception) { errors.add("${HealthMetrics.label(metric)}: ${e.javaClass.simpleName}") }
        }

        part(HealthMetrics.WEIGHT) {
            val since = Instant.ofEpochMilli(ledger.sinceMs(HealthMetrics.WEIGHT, nowMs))
            val recs = hc.readRecords(ReadRecordsRequest(WeightRecord::class, TimeRangeFilter.between(since, now), ascendingOrder = true, pageSize = 100)).records
            for (r in recs) out.add(HealthSample(HealthMetrics.WEIGHT, value = HealthMetrics.kgToLb(r.weight.inKilograms), unit = "lb", ts = r.time.toEpochMilli(), id = idOf(r.metadata.id)))
        }
        part(HealthMetrics.BP) {
            val since = Instant.ofEpochMilli(ledger.sinceMs(HealthMetrics.BP, nowMs))
            val recs = hc.readRecords(ReadRecordsRequest(BloodPressureRecord::class, TimeRangeFilter.between(since, now), ascendingOrder = true, pageSize = 100)).records
            for (r in recs) out.add(HealthSample(HealthMetrics.BP, systolic = Math.round(r.systolic.inMillimetersOfMercury).toInt(), diastolic = Math.round(r.diastolic.inMillimetersOfMercury).toInt(), ts = r.time.toEpochMilli(), id = idOf(r.metadata.id)))
        }
        part(HealthMetrics.STEPS) {
            // today so far, and yesterday's final total (a late sync can still change it; the ledger resends only when the number differs)
            for (back in 0..1) {
                val from = startOfToday.atZone(zone).minusDays(back.toLong()).toInstant()
                val to = if (back == 0) now else startOfToday
                val agg = hc.aggregate(AggregateRequest(setOf(StepsRecord.COUNT_TOTAL), TimeRangeFilter.between(from, to)))
                val n = agg[StepsRecord.COUNT_TOTAL]
                if (n != null && (n > 0 || back == 0)) out.add(HealthSample(HealthMetrics.STEPS, value = n.toDouble(), day = dayOf(from, zone)))
            }
        }
        part(HealthMetrics.SLEEP) {
            val recs = hc.readRecords(ReadRecordsRequest(SleepSessionRecord::class, TimeRangeFilter.between(now.minus(Duration.ofHours(36)), now), ascendingOrder = false, pageSize = 10)).records
            val s = recs.maxByOrNull { it.endTime }
            if (s != null) {
                var asleep = 0L
                var staged = false
                for (st in s.stages) {
                    when (st.stage) {
                        SleepSessionRecord.STAGE_TYPE_DEEP, SleepSessionRecord.STAGE_TYPE_LIGHT, SleepSessionRecord.STAGE_TYPE_REM, SleepSessionRecord.STAGE_TYPE_SLEEPING -> { asleep += Duration.between(st.startTime, st.endTime).toMinutes(); staged = true }
                    }
                }
                if (!staged) asleep = Duration.between(s.startTime, s.endTime).toMinutes()
                if (asleep > 0) out.add(HealthSample(HealthMetrics.SLEEP, minutes = asleep.toInt(), day = dayOf(s.endTime, zone)))       // attributed to the wake day
            }
        }
        part(HealthMetrics.RESTING_HR) {
            val recs = hc.readRecords(ReadRecordsRequest(RestingHeartRateRecord::class, TimeRangeFilter.between(now.minus(Duration.ofHours(48)), now), ascendingOrder = false, pageSize = 5)).records
            recs.firstOrNull()?.let { out.add(HealthSample(HealthMetrics.RESTING_HR, value = it.beatsPerMinute.toDouble(), day = dayOf(it.time, zone))) }
        }
        lastError = errors.joinToString("; ")
        return ledger.filterNew(out)
    }

    private fun idOf(id: String): String? = id.takeIf { it.isNotBlank() }?.let { "hc-$it" }
}

/**
 * Reads and sends health data. Called every 15 minutes by the service, and when the app is opened. Each connection has its own consent (its server's switch and list, and its own
 * ledger of what it was already sent); the phone's switches are shared. A server that did not allow health data is never sent any.
 */
object HealthSync {
    @Volatile var lastRun = 0L
    private val lock = Any()

    fun enabledMetrics(c: Context, slot: Int): Set<String> {
        if (!Prefs.active(c, slot)) return emptySet()
        val s = Prefs.serverSettings(c, slot)
        return HealthMetrics.enabled(s.health, s.healthMetrics, Prefs.ownerHealth(c), Prefs.healthMetrics(c))
    }

    /** Blocking. Returns a short status for the log, or null when there was nothing to do. */
    fun run(c: Context, reason: String): String? = synchronized(lock) {
        var total = 0
        var any = false
        for (slot in Prefs.slots(c)) {
            val metrics = enabledMetrics(c, slot)
            if (metrics.isEmpty()) continue
            any = true
            total += runFor(c, slot, metrics, reason)
        }
        if (!any) return null
        lastRun = System.currentTimeMillis()
        return "sent $total"
    }

    private fun runFor(c: Context, slot: Int, metrics: Set<String>, reason: String): Int {
        val ledger = HealthLedger.parse(Prefs.getFor(c, slot, "health_ledger"))
        val samples = try { HealthReader.read(c, metrics, ledger) } catch (e: Exception) { HealthReader.lastError = e.javaClass.simpleName; emptyList() }
        if (HealthReader.lastError.isNotEmpty()) SentLog.add(c, "health: ${HealthReader.lastError}")
        if (samples.isEmpty()) return 0
        val who = if (Prefs.slots(c).size > 1) " to ${Prefs.name(c, slot)}" else ""
        var sent = 0
        for (chunk in samples.chunked(HealthLedger.MAX_PER_POST)) {
            val r = Api.post(c, slot, "/device/health", HealthLedger.body(chunk))
            if (!r.ok) { SentLog.add(c, "health data not sent$who: ${r.error}"); break }
            ledger.markSent(chunk)
            Prefs.putFor(c, slot, "health_ledger", ledger.serialize())
            sent += chunk.size
        }
        if (sent > 0) SentLog.add(c, "sent $sent health reading${if (sent == 1) "" else "s"}$who (" + samples.take(sent).map { HealthMetrics.label(it.type) }.distinct().joinToString(", ") + ") [$reason]")
        return sent
    }
}

/** The page Android shows when the owner asks what this app does with health data. */
class HealthRationaleActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val t = TextView(this).apply {
            text = "Nori reads the health data you switch on in the Nori app (weight, blood pressure, steps, sleep, resting heart rate) from Health Connect, and sends it to your own Nori server, " +
                "where it is added to your trackers. It is only ever read, never changed, and it goes nowhere else. Each kind is a separate switch in the Nori app, and you can also stop it in Health Connect at any time."
            textSize = 16f
            setPadding(48, 96, 48, 48)
        }
        setContentView(ScrollView(this).apply { addView(t) })
    }
}
