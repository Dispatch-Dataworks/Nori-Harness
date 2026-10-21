package app.nori.companion

import android.app.KeyguardManager
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.BatteryManager
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import app.nori.companion.core.Backoff
import app.nori.companion.core.Command
import app.nori.companion.core.CommandGate
import app.nori.companion.core.CommandTracker
import app.nori.companion.core.MessageInbox
import app.nori.companion.core.NotifyPolicy
import app.nori.companion.core.PollAction
import app.nori.companion.core.PollPolicy
import app.nori.companion.core.PollResponse
import app.nori.companion.core.Protocol
import app.nori.companion.core.ShowOutcome
import app.nori.companion.core.Snapshot
import app.nori.companion.core.Windows
import java.util.concurrent.Callable
import java.util.concurrent.ExecutionException
import java.util.concurrent.Executors
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException

/**
 * The connection to Nori, kept alive as a foreground service (the small ongoing notification is Android's requirement, and carries a Pause button).
 *
 * ONE long-poll loop: POST /device/poll (the server holds it up to 25 s; the client gives up at 40 s). Each request carries the cursor `ack`, which only advances after a
 * message was actually turned into a notification, so a cancelled or failed poll can never lose one. A second, quiet worker runs every 30 s for the things that are not
 * server-driven: closing expired windows, sending place arrivals/departures, and the 15-minute health read.
 *
 * Foreground type is specialUse only: place fences are delivered by Google Play services to a receiver (they need no foreground location service), which also means
 * this service can start at boot and never needs restarting after a location permission is granted.
 */
class SyncService : Service() {
    @Volatile private var running = false
    private var pollThread: Thread? = null
    private val signal = LinkedBlockingQueue<Int>()
    private lateinit var inbox: MessageInbox
    private val commands = CommandTracker()
    private val backoff = Backoff()
    private val dispatcher = Executors.newSingleThreadExecutor { Thread(it, "nori-dispatch").also { t -> t.isDaemon = true } }
    private val runner = Executors.newCachedThreadPool { Thread(it, "nori-command").also { t -> t.isDaemon = true } }
    private var worker: ScheduledExecutorService? = null
    @Volatile private var lastHeartbeat = 0L
    @Volatile private var healthRequested = false
    @Volatile private var lastPollOkAt = 0L

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        Notifier.channels(this)
        inbox = MessageInbox(Prefs.ack(this), MessageInbox.parseSeen(Prefs.seen(this)))
        PlaceManager.forget()
        WindowControl.resume(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        try {
            ServiceCompat.startForeground(this, Notifier.ID_SYNC, Notifier.syncNotification(this), ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } catch (e: Exception) {
            SentLog.add(this, "could not start the background connection: ${e.javaClass.simpleName}")
            stopSelf()
            return START_NOT_STICKY
        }
        if (!running) {
            running = true
            pollThread = Thread({ pollLoop() }, "nori-poll").also { it.isDaemon = true; it.start() }
            worker = Executors.newSingleThreadScheduledExecutor { Thread(it, "nori-work").also { t -> t.isDaemon = true } }.also {
                it.scheduleWithFixedDelay({ safely { periodic() } }, 5, 30, TimeUnit.SECONDS)
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        instance = null
        Api.cancelPoll()
        signal.offer(1)
        worker?.shutdownNow()
        dispatcher.shutdownNow()
        runner.shutdownNow()
        super.onDestroy()
    }

    private inline fun safely(block: () -> Unit) { try { block() } catch (e: Exception) { SentLog.add(this, "background task failed: ${e.javaClass.simpleName}") } }

    // ── the long poll ────────────────────────────────────────────────
    private fun pollLoop() {
        while (running) {
            try {
                when {
                    !Prefs.isPaired(this) -> waitSignal(3_000)
                    Prefs.needsRepair(this) -> waitSignal(30_000)
                    Prefs.paused(this) -> { heartbeat(); waitSignal(60_000) }
                    else -> pollOnce()
                }
            } catch (e: InterruptedException) {
                break
            } catch (e: Exception) {
                SentLog.add(this, "poll error: ${e.javaClass.simpleName}")
                waitSignal(5_000)
            }
        }
    }

    private fun waitSignal(ms: Long) {
        try { signal.poll(ms, TimeUnit.MILLISECONDS); signal.clear() } catch (e: InterruptedException) { /* stopping */ }
    }

    /** Paused: the server is told once a minute, and nothing else leaves the phone. */
    private fun heartbeat() {
        val now = System.currentTimeMillis()
        if (now - lastHeartbeat < 55_000L) return
        lastHeartbeat = now
        val r = Api.post(this, "/device/poll", Protocol.pausedHeartbeat(BuildConfig.VERSION_NAME, inbox.ackValue()), 15_000)
        handleHttpFailure(r)
    }

    private fun handleHttpFailure(r: Api.Result): Boolean {
        when (r.action) {
            PollAction.OK -> return false
            PollAction.RE_PAIR -> {
                if (!Prefs.needsRepair(this)) { Prefs.setNeedsRepair(this, true); SentLog.add(this, "the server no longer knows this phone (401): pair it again"); PlaceManager.sync(this); Notifier.refreshSync(this) }
                Prefs.setStatus(this, "This phone was removed on the server: pair it again.")
            }
            PollAction.BACK_OFF_LONG -> Prefs.setStatus(this, "Switched off on the server for now: trying again in a minute.")
            PollAction.SLOW_DOWN -> Prefs.setStatus(this, "The server asked to slow down.")
            PollAction.RETRY -> Prefs.setStatus(this, "Can't reach Nori: ${r.error}")
        }
        return true
    }

    private fun pollOnce() {
        val now = System.currentTimeMillis()
        val body = Protocol.pollRequest(
            BuildConfig.VERSION_NAME, false, AppState.appVisible, inbox.ackValue(), deviceCaps(), Windows.reportedUntil(Prefs.interactUntil(this), now),
            Windows.reportedUntil(Prefs.shareUntil(this), now), snapshot(),
        )
        val r = Api.post(this, "/device/poll", body, PollPolicy.CLIENT_TIMEOUT_MS, isPoll = true)
        if (!running) return
        if (r.code == 0 && Api.wasCancelled()) return            // we hung up on purpose (a switch or window changed); nothing was lost, poll again at once
        val resp = if (r.ok) Protocol.parsePoll(r.text) else null
        if (resp == null) {
            val failure = if (r.ok) Api.Result(0, null, "", "an unreadable answer from the server") else r
            handleHttpFailure(failure)
            val wait = backoff.wait(failure.action)
            if (failure.action == PollAction.OK) backoff.success()
            waitSignal(wait.coerceAtLeast(PollPolicy.MIN_LOOP_MS))
            return
        }
        backoff.success()
        lastPollOkAt = System.currentTimeMillis()
        applyResponse(resp)
        if (System.currentTimeMillis() - now < PollPolicy.MIN_LOOP_MS) waitSignal(PollPolicy.MIN_LOOP_MS - (System.currentTimeMillis() - now))
    }

    private fun applyResponse(resp: PollResponse) {
        Prefs.setStatus(this, "Connected")
        resp.settings?.let { s ->
            val json = s.toJson().toString()
            if (json != Prefs.serverSettingsRaw(this)) {
                Prefs.setServerSettings(this, json)
                workNow(this)                                              // fences, health schedule: pick up the change now
            }
        }
        resp.app?.let { AppUpdater.noteFromPoll(this, it) }

        val settings = Prefs.serverSettings(this)
        val owner = Prefs.owner(this)
        var failed = false
        val shown = inbox.process(resp.messages) { m ->
            if (!NotifyPolicy.decide(m, settings.notifications, owner.notifications, settings.quiet, Prefs.paused(this))) ShowOutcome.SKIPPED
            else Notifier.showMessage(this, m).also { if (it == ShowOutcome.FAILED) failed = true }
        }
        if (resp.messages.isNotEmpty()) {
            Prefs.setInbox(this, inbox.ackValue(), inbox.serializeSeen())      // remembered before anything else can go wrong
            if (shown > 0) SentLog.add(this, "showed $shown notification${if (shown == 1) "" else "s"} from Nori")
        }
        if (failed) {
            Prefs.setStatus(this, "Notifications are off for Nori: ${resp.messages.size} message(s) are waiting. Allow notifications in Android settings.")
            waitSignal(30_000)                                                  // not acknowledged, so the server will send them again once they can be shown
        }
        resp.command?.let { handleCommand(it) }
    }

    // ── commands ─────────────────────────────────────────────────────
    private fun handleCommand(cmd: Command) {
        val app = applicationContext
        Thread { Api.post(app, "/device/ack", Protocol.ack(cmd.id), 10_000) }.also { it.isDaemon = true }.start()   // confirm receipt at once; the server resends an un-acked command
        when (commands.begin(cmd)) {
            CommandTracker.Verdict.IN_FLIGHT, CommandTracker.Verdict.DONE -> return
            CommandTracker.Verdict.NEW -> {}
        }
        dispatcher.execute {
            val fut = runner.submit(Callable { CommandExecutor.execute(app, cmd) })
            val body = try {
                fut.get(22, TimeUnit.SECONDS)                                         // always answer within 25 s
            } catch (e: TimeoutException) {
                fut.cancel(true); Protocol.resultError(cmd.id, "timed out")
            } catch (e: ExecutionException) {
                Protocol.resultError(cmd.id, "the phone could not do that (${e.cause?.javaClass?.simpleName ?: "error"})")
            } catch (e: Exception) {
                Protocol.resultError(cmd.id, "the phone could not do that")
            }
            val r = Api.post(app, "/device/result", body, 30_000)
            if (!r.ok) SentLog.add(app, "the answer to command ${cmd.action} did not get through: ${r.error}")
            commands.finish(cmd)
        }
    }

    // ── what the phone reports about itself ──────────────────────────
    private fun deviceCaps() = CommandGate.deviceCaps(
        Prefs.owner(this), NoriAccessibilityService.running(this), true, Notifier.canNotify(this), healthReady, PlaceManager.ready(this),
    )

    @Volatile private var healthReady = false

    private fun snapshot(): Snapshot {
        val bm = getSystemService(Context.BATTERY_SERVICE) as BatteryManager
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        val km = getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        val cm = getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val caps = try { cm.getNetworkCapabilities(cm.activeNetwork) } catch (e: Exception) { null }
        val net = when {
            caps == null -> "none"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) -> "cellular"
            caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> "ethernet"
            else -> "other"
        }
        val dnd = try { nm.currentInterruptionFilter.let { it != NotificationManager.INTERRUPTION_FILTER_ALL && it != NotificationManager.INTERRUPTION_FILTER_UNKNOWN } } catch (e: Exception) { false }
        val place = if (PlaceManager.wanted(this)) Prefs.currentPlace(this).ifEmpty { null } else null
        return Snapshot(bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY).takeIf { it in 0..100 }, bm.isCharging, net, pm.isInteractive, km.isKeyguardLocked, dnd, place)
    }

    // ── the quiet worker ─────────────────────────────────────────────
    private fun periodic() {
        val app = applicationContext
        WindowControl.reconcile(app)
        if (!Prefs.isPaired(app) || Prefs.needsRepair(app)) return
        PlaceManager.sync(app)
        sendPlaceEvents(app)
        val due = System.currentTimeMillis() - HealthSync.lastRun >= 15 * 60_000L
        val metrics = HealthSync.enabledMetrics(app)
        healthReady = metrics.isNotEmpty() && (try { HealthReader.available(app) && HealthReader.grantedMetrics(app).isNotEmpty() } catch (e: Exception) { false })
        if (!Prefs.paused(app) && metrics.isNotEmpty() && (due || healthRequested)) {
            val reason = if (healthRequested) "app opened" else "every 15 minutes"
            healthRequested = false
            HealthSync.run(app, reason)
        }
    }

    private fun sendPlaceEvents(c: Context) {
        if (!PlaceManager.wanted(c)) return
        PlaceState.flush(c, System.currentTimeMillis())
        while (true) {
            val outbox = PlaceState.outbox(c)
            val batch = outbox.peek()
            if (batch.isEmpty()) return
            val r = Api.post(c, "/device/events", outbox.body())
            if (!r.ok) { if (r.code != 403) SentLog.add(c, "place events not sent: ${r.error}"); return }
            PlaceState.dropSent(c, batch.size)
            SentLog.add(c, "told Nori about ${batch.size} place change${if (batch.size == 1) "" else "s"} (arrived/left only)")
        }
    }

    companion object {
        @Volatile private var instance: SyncService? = null

        fun start(c: Context) {
            try { ContextCompat.startForegroundService(c.applicationContext, Intent(c.applicationContext, SyncService::class.java)) }
            catch (e: Exception) { SentLog.add(c, "could not start the background connection: ${e.javaClass.simpleName}") }
        }

        fun stop(c: Context) { c.applicationContext.stopService(Intent(c.applicationContext, SyncService::class.java)) }

        /** A switch, window or visibility changed: hang up the current long poll and ask again at once (anything unacknowledged is simply sent again). */
        fun pollNow(c: Context) {
            val s = instance ?: return
            Api.cancelPoll()
            s.signal.offer(1)
        }

        /** Run the quiet worker's tasks soon (fences changed, a place event is waiting). */
        fun workNow(c: Context) {
            val s = instance ?: return
            s.worker?.execute { s.safely { s.periodic() } }
        }

        /** The app was opened: read health data now as well as every 15 minutes. */
        fun requestHealth(c: Context) {
            val s = instance ?: return
            s.healthRequested = true
            workNow(c)
        }
    }
}
