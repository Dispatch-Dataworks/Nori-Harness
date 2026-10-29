package app.nori.companion

import android.app.KeyguardManager
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
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
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.ExecutionException
import java.util.concurrent.Executors
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException

/**
 * The connection to Nori, kept alive as a foreground service (the small ongoing notification is Android's requirement, and carries a Pause button).
 *
 * One long-poll loop PER CONNECTION (the phone can be paired with several servers): POST /device/poll (the server holds it up to 25 s; the client gives up at 40 s). Each request
 * carries that server's cursor `ack`, which only advances after a message was actually turned into a notification, so a cancelled or failed poll can never lose one. The loops are
 * independent: one server being down never delays another. A second, quiet worker runs every 30 s for the things that are not server-driven: closing expired windows, sending place
 * arrivals/departures, and the 15-minute health read.
 *
 * Commands from any server run one at a time, so two assistants can never drive the screen at once.
 *
 * Foreground type is specialUse only: place fences are delivered by Google Play services to a receiver (they need no foreground location service), which also means
 * this service can start at boot and never needs restarting after a location permission is granted.
 */
class SyncService : Service() {
    @Volatile private var running = false
    private val lines = ConcurrentHashMap<Int, Line>()
    private val dispatcher = Executors.newSingleThreadExecutor { Thread(it, "nori-dispatch").also { t -> t.isDaemon = true } }
    private val runner = Executors.newCachedThreadPool { Thread(it, "nori-command").also { t -> t.isDaemon = true } }
    private var worker: ScheduledExecutorService? = null
    @Volatile private var healthRequested = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        Notifier.channels(this)
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
            worker = Executors.newSingleThreadScheduledExecutor { Thread(it, "nori-work").also { t -> t.isDaemon = true } }.also {
                it.scheduleWithFixedDelay({ safely { periodic() } }, 5, 30, TimeUnit.SECONDS)
            }
        }
        syncLines()
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        instance = null
        for (l in lines.values) l.stop()
        lines.clear()
        Api.cancelPoll()
        worker?.shutdownNow()
        dispatcher.shutdownNow()
        runner.shutdownNow()
        super.onDestroy()
    }

    private inline fun safely(block: () -> Unit) { try { block() } catch (e: Exception) { SentLog.add(this, "background task failed: ${e.javaClass.simpleName}") } }

    /** One loop for every connection the phone has, and none for one that was removed. Safe to call as often as you like. */
    private fun syncLines() {
        val want = Prefs.slots(this).toSet()
        for (s in want) lines.computeIfAbsent(s) { Line(it).also { l -> l.start() } }
        for ((s, l) in lines.entries.toList()) if (s !in want) { l.stop(); lines.remove(s) }
    }

    /** " (Iron)" when the phone has more than one connection, so the sent log says which server a line is about. */
    private fun who(slot: Int) = if (Prefs.slots(this).size > 1) " (${Prefs.name(this, slot)})" else ""

    // ── one connection's long poll ───────────────────────────────────
    private inner class Line(val slot: Int) {
        @Volatile private var alive = true
        val signal = LinkedBlockingQueue<Int>()
        private val thread = Thread({ pollLoop() }, "nori-poll-$slot").also { it.isDaemon = true }
        private var inbox = MessageInbox(0, emptySet())
        private var commands = CommandTracker()
        private var backoff = Backoff()
        private var tokenSeen: String? = null
        @Volatile private var lastHeartbeat = 0L

        fun start() = thread.start()

        fun stop() {
            alive = false
            Api.cancelPoll(slot)
            signal.offer(1)
        }

        private fun live() = running && alive

        /** What was remembered belongs to the pairing it was learnt under: a new pairing (the same slot paired again) starts from its own cursor, not the old one's. */
        private fun reloadIfRepaired() {
            val tok = Prefs.token(this@SyncService, slot)
            if (tok == tokenSeen) return
            tokenSeen = tok
            inbox = MessageInbox(Prefs.ack(this@SyncService, slot), MessageInbox.parseSeen(Prefs.seen(this@SyncService, slot)))
            commands = CommandTracker()
            backoff = Backoff()
        }

        private fun pollLoop() {
            while (live()) {
                try {
                    reloadIfRepaired()
                    when {
                        !Prefs.isPaired(this@SyncService, slot) -> waitSignal(3_000)
                        Prefs.needsRepair(this@SyncService, slot) -> waitSignal(30_000)
                        Prefs.paused(this@SyncService) -> { heartbeat(); waitSignal(60_000) }
                        else -> pollOnce()
                    }
                } catch (e: InterruptedException) {
                    break
                } catch (e: Exception) {
                    SentLog.add(this@SyncService, "poll error${who(slot)}: ${e.javaClass.simpleName}")
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
            val r = Api.post(this@SyncService, slot, "/device/poll", Protocol.pausedHeartbeat(BuildConfig.VERSION_NAME, inbox.ackValue()), 15_000)
            handleHttpFailure(r)
        }

        private fun handleHttpFailure(r: Api.Result): Boolean {
            val c = this@SyncService
            when (r.action) {
                PollAction.OK -> return false
                PollAction.RE_PAIR -> {
                    if (!Prefs.needsRepair(c, slot)) {
                        Prefs.setNeedsRepair(c, slot, true)
                        SentLog.add(c, "the server no longer knows this phone${who(slot)} (401): pair it again")
                        PlaceManager.sync(c); Notifier.refreshSync(c)
                    }
                    Prefs.setStatus(c, slot, "This phone was removed on the server: pair it again.")
                }
                PollAction.BACK_OFF_LONG -> Prefs.setStatus(c, slot, "Switched off on the server for now: trying again in a minute.")
                PollAction.SLOW_DOWN -> Prefs.setStatus(c, slot, "The server asked to slow down.")
                PollAction.RETRY -> Prefs.setStatus(c, slot, "Can't reach Nori: ${r.error}")
            }
            return true
        }

        private fun pollOnce() {
            val c = this@SyncService
            val now = System.currentTimeMillis()
            val body = Protocol.pollRequest(
                BuildConfig.VERSION_NAME, false, AppState.chatVisibleFor(slot), inbox.ackValue(), deviceCaps(), Windows.reportedUntil(Prefs.interactUntil(c), now),
                Windows.reportedUntil(Prefs.shareUntil(c), now), snapshot(slot),
            )
            val r = Api.post(c, slot, "/device/poll", body, PollPolicy.CLIENT_TIMEOUT_MS, isPoll = true)
            if (!live()) return
            if (r.code == 0 && Api.wasCancelled(slot)) return            // we hung up on purpose (a switch or window changed); nothing was lost, poll again at once
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
            applyResponse(resp)
            if (System.currentTimeMillis() - now < PollPolicy.MIN_LOOP_MS) waitSignal(PollPolicy.MIN_LOOP_MS - (System.currentTimeMillis() - now))
        }

        private fun applyResponse(resp: PollResponse) {
            val c = this@SyncService
            Prefs.setStatus(c, slot, "Connected")
            resp.settings?.let { s ->
                val json = s.toJson().toString()
                if (json != Prefs.serverSettingsRaw(c, slot)) {
                    Prefs.setServerSettings(c, slot, json)
                    workNow(c)                                              // fences, health schedule: pick up the change now
                }
            }
            resp.app?.let { AppUpdater.noteFromPoll(c, slot, it) }

            val settings = Prefs.serverSettings(c, slot)
            val owner = Prefs.owner(c)
            var failed = false
            val shown = inbox.process(resp.messages) { m ->
                if (!NotifyPolicy.decide(m, settings.notifications, owner.notifications, settings.quiet, Prefs.paused(c))) ShowOutcome.SKIPPED
                else Notifier.showMessage(c, slot, m).also { if (it == ShowOutcome.FAILED) failed = true }
            }
            if (resp.messages.isNotEmpty()) {
                Prefs.setInbox(c, slot, inbox.ackValue(), inbox.serializeSeen())      // remembered before anything else can go wrong
                if (shown > 0) SentLog.add(c, "showed $shown notification${if (shown == 1) "" else "s"} from Nori${who(slot)}")
            }
            if (failed) {
                Prefs.setStatus(c, slot, "Notifications are off for Nori: ${resp.messages.size} message(s) are waiting. Allow notifications in Android settings.")
                waitSignal(30_000)                                                  // not acknowledged, so the server will send them again once they can be shown
            }
            resp.command?.let { handleCommand(it) }
        }

        // ── commands ─────────────────────────────────────────────────
        private fun handleCommand(cmd: Command) {
            val app = applicationContext
            val received = System.currentTimeMillis()
            Thread { Api.post(app, slot, "/device/ack", Protocol.ack(cmd.id), 10_000) }.also { it.isDaemon = true }.start()   // confirm receipt at once; the server resends an un-acked command
            val tracker = commands
            when (tracker.begin(cmd)) {
                CommandTracker.Verdict.IN_FLIGHT, CommandTracker.Verdict.DONE -> return
                CommandTracker.Verdict.NEW -> {}
            }
            dispatcher.execute {
                // The server wants an answer within 25 s of asking. Time spent waiting behind another connection's command counts against that.
                val budget = 22_000L - (System.currentTimeMillis() - received)
                val body = if (budget < 1_000L) Protocol.resultError(cmd.id, "the phone was busy with another request: ask again")
                else {
                    val fut = runner.submit(Callable { CommandExecutor.execute(app, slot, cmd) })
                    try {
                        fut.get(budget, TimeUnit.MILLISECONDS)
                    } catch (e: TimeoutException) {
                        fut.cancel(true); Protocol.resultError(cmd.id, "timed out")
                    } catch (e: ExecutionException) {
                        Protocol.resultError(cmd.id, "the phone could not do that (${e.cause?.javaClass?.simpleName ?: "error"})")
                    } catch (e: Exception) {
                        Protocol.resultError(cmd.id, "the phone could not do that")
                    }
                }
                val r = Api.post(app, slot, "/device/result", body, 30_000)
                if (!r.ok) SentLog.add(app, "the answer to command ${cmd.action}${who(slot)} did not get through: ${r.error}")
                tracker.finish(cmd)
            }
        }
    }

    // ── what the phone reports about itself ──────────────────────────
    private fun deviceCaps() = CommandGate.deviceCaps(
        Prefs.owner(this), NoriAccessibilityService.running(this), true, Notifier.canNotify(this), healthReady, PlaceManager.ready(this),
    )

    @Volatile private var healthReady = false

    private fun snapshot(slot: Int): Snapshot {
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
        val place = if (PlaceManager.wanted(this, slot)) Prefs.currentPlace(this, slot).ifEmpty { null } else null
        return Snapshot(bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY).takeIf { it in 0..100 }, bm.isCharging, net, pm.isInteractive, km.isKeyguardLocked, dnd, place)
    }

    // ── the quiet worker ─────────────────────────────────────────────
    private fun periodic() {
        val app = applicationContext
        WindowControl.reconcile(app)
        val usable = Prefs.usableSlots(app)
        if (usable.isEmpty()) return
        PlaceManager.sync(app)
        for (slot in usable) sendPlaceEvents(app, slot)
        val due = System.currentTimeMillis() - HealthSync.lastRun >= 15 * 60_000L
        val wantsHealth = usable.any { HealthSync.enabledMetrics(app, it).isNotEmpty() }
        healthReady = wantsHealth && (try { HealthReader.available(app) && HealthReader.grantedMetrics(app).isNotEmpty() } catch (e: Exception) { false })
        if (!Prefs.paused(app) && wantsHealth && (due || healthRequested)) {
            val reason = if (healthRequested) "app opened" else "every 15 minutes"
            healthRequested = false
            HealthSync.run(app, reason)
        }
    }

    private fun sendPlaceEvents(c: Context, slot: Int) {
        if (!PlaceManager.wanted(c, slot)) return
        PlaceState.flush(c, slot, System.currentTimeMillis())
        while (true) {
            val outbox = PlaceState.outbox(c, slot)
            val batch = outbox.peek()
            if (batch.isEmpty()) return
            val r = Api.post(c, slot, "/device/events", outbox.body())
            if (!r.ok) { if (r.code != 403) SentLog.add(c, "place events not sent${who(slot)}: ${r.error}"); return }
            PlaceState.dropSent(c, slot, batch.size)
            SentLog.add(c, "told Nori${who(slot)} about ${batch.size} place change${if (batch.size == 1) "" else "s"} (arrived/left only)")
        }
    }

    companion object {
        @Volatile private var instance: SyncService? = null

        fun start(c: Context) {
            try { ContextCompat.startForegroundService(c.applicationContext, Intent(c.applicationContext, SyncService::class.java)) }
            catch (e: Exception) { SentLog.add(c, "could not start the background connection: ${e.javaClass.simpleName}") }
        }

        fun stop(c: Context) { c.applicationContext.stopService(Intent(c.applicationContext, SyncService::class.java)) }

        /** A connection was added or removed: start a poll loop for the new one, end the one that is gone. */
        fun refresh(c: Context) { instance?.syncLines() }

        /** A switch, window or visibility changed: hang up every current long poll and ask again at once (anything unacknowledged is simply sent again). */
        fun pollNow(c: Context) {
            val s = instance ?: return
            Api.cancelPoll()
            for (l in s.lines.values) l.signal.offer(1)
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
