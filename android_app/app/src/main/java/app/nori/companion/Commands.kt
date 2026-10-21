package app.nori.companion

import android.app.KeyguardManager
import android.content.Context
import android.content.Intent
import android.graphics.PixelFormat
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.AlarmClock
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import app.nori.companion.core.ActionArgs
import app.nori.companion.core.AppGuard
import app.nori.companion.core.Command
import app.nori.companion.core.CommandGate
import app.nori.companion.core.DeviceSettings
import app.nori.companion.core.GateState
import app.nori.companion.core.HourlyCap
import app.nori.companion.core.Protocol
import app.nori.companion.core.UrlGuard
import app.nori.companion.core.Windows
import org.json.JSONArray
import org.json.JSONObject

/** The intent-based phone actions: open a link, open an app, a timer, an alarm, directions, the list of apps. Every input is validated by the plain-Kotlin core first. */
object ActionRunner {
    private val main = Handler(Looper.getMainLooper())

    fun screenReady(c: Context): String? {
        val pm = c.getSystemService(Context.POWER_SERVICE) as PowerManager
        val km = c.getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager
        if (!pm.isInteractive || km.isKeyguardLocked) return "the screen is off or locked, so nothing was opened"
        return null
    }

    /**
     * Starts an activity. Android blocks background activity starts, except for an app that has a visible window: with "Display over other apps" allowed, a tiny
     * invisible window is shown for a moment first. That is the ONLY reason this app asks for that permission (and for keeping the screen on during a window).
     */
    private fun launch(c: Context, intent: Intent): String? {
        if (!AppState.appVisible && !Settings.canDrawOverlays(c)) return "Android blocks opening things from the background: allow \"Display over other apps\" for Nori (Nori app -> Battery & permissions)"
        val wm = c.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val v = View(c)
        val lp = WindowManager.LayoutParams(1, 1, WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS, PixelFormat.TRANSLUCENT).apply { gravity = Gravity.TOP or Gravity.START }
        var error: String? = null
        val done = java.util.concurrent.CountDownLatch(1)
        main.post {
            try { if (Settings.canDrawOverlays(c)) wm.addView(v, lp) } catch (e: Exception) { /* permission revoked */ }
            try { c.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) } catch (e: Exception) { error = if (e is android.content.ActivityNotFoundException) "no app on this phone can do that" else "could not start it: ${e.javaClass.simpleName}" }
            main.postDelayed({ try { wm.removeView(v) } catch (e: Exception) { /* gone */ } }, 3000)
            done.countDown()
        }
        done.await(5, java.util.concurrent.TimeUnit.SECONDS)
        return error
    }

    private fun launcherApps(c: Context): List<Pair<String, String>> {
        val pm = c.packageManager
        val main = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        val seen = HashSet<String>()
        val out = ArrayList<Pair<String, String>>()
        for (r in pm.queryIntentActivities(main, 0)) {
            val pkg = r.activityInfo.packageName
            if (seen.add(pkg)) out.add(pkg to r.loadLabel(pm).toString())
        }
        return out
    }

    fun run(c: Context, cmd: Command, s: DeviceSettings): Outcome {
        val args = cmd.args
        return when (cmd.action) {
            "list_apps" -> {
                val arr = JSONArray()
                for ((pkg, label) in launcherApps(c).filter { AppGuard.listable(it.first, s.hiddenApps, s.neverControlApps) }.sortedBy { it.second.lowercase() }.take(400)) {
                    arr.put(JSONObject().put("package", pkg).put("name", label))
                }
                Outcome.Ok(JSONObject().put("apps", arr))
            }
            "open_url" -> {
                screenReady(c)?.let { return Outcome.Refused(it) }
                val v = UrlGuard.check(args.optString("url"))
                if (!v.ok) return Outcome.Refused("refused: " + v.reason)
                val err = launch(c, Intent(Intent.ACTION_VIEW, Uri.parse(v.url)).addCategory(Intent.CATEGORY_BROWSABLE))
                if (err != null) Outcome.Refused(err) else Outcome.Ok(JSONObject().put("ok", true))
            }
            "open_app" -> {
                screenReady(c)?.let { return Outcome.Refused(it) }
                val t = ActionArgs.appTarget(args)
                if (t.error != null) return Outcome.Refused(t.error)
                val pkg = t.value!!.pkg ?: run {
                    val visible = launcherApps(c).filter { AppGuard.listable(it.first, s.hiddenApps, s.neverControlApps) }
                    val r = ActionArgs.resolveByName(t.value.name ?: "", visible)
                    if (r.error != null) return Outcome.Refused(r.error)
                    r.value!!
                }
                if (!AppGuard.listable(pkg, s.hiddenApps, s.neverControlApps)) return Outcome.Refused("that app is not available to Nori")
                val intent = c.packageManager.getLaunchIntentForPackage(pkg) ?: return Outcome.Refused("that app cannot be opened")
                val label = try { c.packageManager.getApplicationLabel(c.packageManager.getApplicationInfo(pkg, 0)).toString() } catch (e: Exception) { pkg }
                val err = launch(c, intent)
                if (err != null) Outcome.Refused(err) else Outcome.Ok(JSONObject().put("ok", true).put("app", label))
            }
            "set_timer" -> {
                val t = ActionArgs.timer(args)
                if (t.error != null) return Outcome.Refused(t.error)
                val i = Intent(AlarmClock.ACTION_SET_TIMER).putExtra(AlarmClock.EXTRA_LENGTH, t.value!!.seconds).putExtra(AlarmClock.EXTRA_SKIP_UI, true)
                if (t.value.label.isNotEmpty()) i.putExtra(AlarmClock.EXTRA_MESSAGE, t.value.label)
                val err = launch(c, i)
                if (err != null) Outcome.Refused(err) else Outcome.Ok(JSONObject().put("ok", true))
            }
            "set_alarm" -> {
                val a = ActionArgs.alarm(args)
                if (a.error != null) return Outcome.Refused(a.error)
                val i = Intent(AlarmClock.ACTION_SET_ALARM).putExtra(AlarmClock.EXTRA_HOUR, a.value!!.hour).putExtra(AlarmClock.EXTRA_MINUTES, a.value.minute).putExtra(AlarmClock.EXTRA_SKIP_UI, true)
                if (a.value.label.isNotEmpty()) i.putExtra(AlarmClock.EXTRA_MESSAGE, a.value.label)
                if (a.value.days.isNotEmpty()) i.putIntegerArrayListExtra(AlarmClock.EXTRA_DAYS, ArrayList(a.value.days))
                val err = launch(c, i)
                if (err != null) Outcome.Refused(err) else Outcome.Ok(JSONObject().put("ok", true))
            }
            "navigate" -> {
                screenReady(c)?.let { return Outcome.Refused(it) }
                val u = ActionArgs.navigateUri(args)
                if (u.error != null) return Outcome.Refused(u.error)
                val err = launch(c, Intent(Intent.ACTION_VIEW, Uri.parse(u.value!!)))
                if (err != null) Outcome.Refused(err) else Outcome.Ok(JSONObject().put("ok", true))
            }
            else -> Outcome.Refused("unknown action")
        }
    }
}

/**
 * Runs one server command on the phone, behind every check the contract lists, whatever the server says: pause, the two switches, the open window, the app in front,
 * the `why`, the hourly cap, the URL rules, and (for risky controls) the owner's approval. Returns the /device/result body.
 */
object CommandExecutor {
    private val actionCap by lazy { HourlyCap.parse(Prefs.get(appCtx!!, "cap_actions")) }
    private val interactCap by lazy { HourlyCap.parse(Prefs.get(appCtx!!, "cap_interact")) }
    @Volatile private var appCtx: Context? = null

    private fun gateState(c: Context): GateState = GateState(
        now = System.currentTimeMillis(), paused = Prefs.paused(c), server = Prefs.serverSettings(c), owner = Prefs.owner(c),
        interactUntil = Prefs.interactUntil(c), shareUntil = Prefs.shareUntil(c),
        accessibilityRunning = NoriAccessibilityService.running(c), projectionRunning = ScreenShareService.active(),
    )

    fun execute(c: Context, cmd: Command): JSONObject {
        appCtx = c.applicationContext
        val body = try { run(c.applicationContext, cmd) } catch (e: Exception) { Protocol.resultError(cmd.id, "the phone could not do that (${e.javaClass.simpleName})") }
        val tag = if (body.optBoolean("needs_approval")) "needs your approval" else if (body.optBoolean("ok")) "done" else "refused (${body.optString("error")})"
        SentLog.add(c, ("command ${cmd.action}: $tag").take(300))
        return body
    }

    private fun run(c: Context, cmd: Command): JSONObject {
        val st = gateState(c)
        CommandGate.check(cmd.action, st)?.let { return Protocol.resultError(cmd.id, it) }
        val s = st.server
        val guard = { pkg: String? -> AppGuard.check(pkg, s.hiddenApps, s.neverControlApps) }
        val now = st.now
        val isInteract = cmd.action == "interact"
        val counted = cmd.action in CommandGate.COUNTED

        if (counted && !actionCap.wouldAllow(now, s.actionsPerHour)) return Protocol.resultError(cmd.id, "the hourly limit of ${s.actionsPerHour} phone actions has been reached; try again later")
        val iLimit = CommandGate.interactLimit(s.actionsPerHour)
        if (isInteract && !interactCap.wouldAllow(now, iLimit)) return Protocol.resultError(cmd.id, "the hourly limit of $iLimit control steps has been reached; try again later")

        val outcome: Outcome = when {
            cmd.action in CommandGate.ACTION_COMMANDS -> ActionRunner.run(c, cmd, s)
            cmd.action == "read_screen" -> {
                val svc = NoriAccessibilityService.instance ?: return Protocol.resultError(cmd.id, "the accessibility service is not connected")
                svc.readScreen(guard)
            }
            isInteract -> {
                val svc = NoriAccessibilityService.instance ?: return Protocol.resultError(cmd.id, "the accessibility service is not connected")
                val why = cmd.args.optString("why", "").trim()
                if (why.isEmpty()) return Protocol.resultError(cmd.id, "interact needs a non-empty `why`: say why you are doing this")
                val o = svc.interact(cmd.args, cmd.approved, cmd.forceApproval, guard)
                if (o is Outcome.Ok) SentLog.add(c, ("control step: " + o.result.optString("did") + " | why: " + why).take(300))
                o
            }
            cmd.action == CommandGate.SCREENSHOT -> screenshot(c, guard)
            else -> Outcome.Refused("unknown action")
        }
        if (outcome is Outcome.Ok) {
            if (counted) { actionCap.tryTake(now, s.actionsPerHour); Prefs.put(c, "cap_actions", actionCap.serialize()) }
            if (isInteract) { interactCap.tryTake(now, iLimit); Prefs.put(c, "cap_interact", interactCap.serialize()) }
        }
        return when (outcome) {
            is Outcome.Ok -> Protocol.resultOk(cmd.id, outcome.result)
            is Outcome.Refused -> Protocol.resultError(cmd.id, outcome.error)
            is Outcome.NeedsApproval -> Protocol.resultNeedsApproval(cmd.id, outcome.reason, outcome.describe, outcome.fingerprint)
        }
    }

    /** One picture, only now, only inside the open window, never of a private or deny-listed app (checked before AND after the frame is taken). */
    private fun screenshot(c: Context, guard: (String?) -> AppGuard.Verdict): Outcome {
        val svc = NoriAccessibilityService.instance ?: return Outcome.Refused("the accessibility service is not connected, so the phone cannot tell which app is in front")
        val share = ScreenShareService.current ?: return Outcome.Refused("screen capture is not running on the phone")
        val before = svc.foregroundPackage()
        val v1 = guard(before)
        if (!v1.allowed) return Outcome.Refused(v1.reason)
        val shot = share.grab()
        val b64 = shot.jpegB64 ?: return Outcome.Refused(shot.error ?: "capture failed")
        val after = svc.foregroundPackage()
        val v2 = guard(after)
        if (!v2.allowed) return Outcome.Refused("the app in front changed while the picture was taken, so it was thrown away")
        val label = try { c.packageManager.getApplicationLabel(c.packageManager.getApplicationInfo(before!!, 0)).toString() } catch (e: Exception) { before ?: "" }
        return Outcome.Ok(JSONObject().put("jpeg_b64", b64).put("app", label).put("package", before ?: ""))
    }
}
