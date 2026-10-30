package app.nori.companion

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.text.InputType
import android.view.ViewGroup
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import app.nori.companion.core.AgentName
import app.nori.companion.core.Connections
import app.nori.companion.core.DeviceSettings
import app.nori.companion.core.Geo
import app.nori.companion.core.HealthMetrics
import app.nori.companion.core.Protocol
import app.nori.companion.core.StatusLine
import app.nori.companion.core.Windows
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import org.json.JSONObject

/**
 * Everything about what this phone lets Nori do, in one place: the connections (one per Nori server this phone is paired with), one switch per capability (each is the phone's own half;
 * every server has its own), the two windows you open yourself, the list of everything this app sent, and help with Android's permissions. Nothing beyond notifications is on until you turn it on here.
 * The switches and windows are the phone's, so they apply to every connection alike.
 */
class SettingsActivity : Activity() {
    private lateinit var box: LinearLayout
    private val REQ_NOTIF = 81
    private val REQ_LOC = 83
    private val REQ_BGLOC = 84
    private val REQ_HEALTH = 87
    private val REQ_LOC_SAVE = 88

    private val bg = Color.parseColor("#111111")
    private val fg = Color.parseColor("#EEEEEE")
    private val dim = Color.parseColor("#9AA0A6")
    private val accent = Color.parseColor("#7FD1AE")
    private var typedServer = ""
    private var saveSlot = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        window.setBackgroundDrawable(android.graphics.drawable.ColorDrawable(bg))
        val scroll = ScrollView(this)
        box = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(18), dp(12), dp(18), dp(40)) }
        scroll.addView(box, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        setContentView(scroll)
        ViewCompat.setOnApplyWindowInsetsListener(scroll) { v, wi ->
            val i = wi.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime())
            v.setPadding(i.left, i.top, i.right, i.bottom)
            WindowInsetsCompat.CONSUMED
        }
        typedServer = ""
    }

    override fun onResume() {
        super.onResume()
        render()
        if (Prefs.anyPaired(this)) { SyncService.start(this); SyncService.refresh(this) }
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()
    private fun toast(s: String) = Toast.makeText(this, s, Toast.LENGTH_LONG).show()
    private fun later(ms: Long = 500, f: () -> Unit) { box.postDelayed(f, ms) }

    private fun title(s: String) = TextView(this).apply { text = s; textSize = 22f; setTextColor(fg); setTypeface(null, Typeface.BOLD); setPadding(0, dp(8), 0, dp(4)) }
    private fun head(s: String) = TextView(this).apply { text = s; textSize = 16f; setTextColor(accent); setTypeface(null, Typeface.BOLD); setPadding(0, dp(24), 0, dp(4)) }
    private fun body(s: String) = TextView(this).apply { text = s; textSize = 14f; setTextColor(dim); setPadding(0, dp(2), 0, dp(6)) }
    private fun line(s: String, strong: Boolean = false) = TextView(this).apply { text = s; textSize = 15f; setTextColor(fg); if (strong) setTypeface(null, Typeface.BOLD) }
    private fun button(s: String, onClick: () -> Unit) = Button(this).apply { text = s; isAllCaps = false; setOnClickListener { onClick() } }
    private fun field(hint: String, value: String, code: Boolean = false) = EditText(this).apply {
        this.hint = hint; setText(value); setTextColor(fg); setHintTextColor(dim); textSize = 15f
        inputType = if (code) InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS else InputType.TYPE_TEXT_VARIATION_URI
        isSingleLine = true
    }
    private fun toggle(label: String, on: Boolean, onChange: (Boolean) -> Unit) = Switch(this).apply {
        text = label; isChecked = on; setTextColor(fg); textSize = 15f; setPadding(0, dp(8), 0, dp(8))
        setOnCheckedChangeListener { _, v -> onChange(v) }
    }

    /** A row of "15 min / 1 h / 3 h" buttons. */
    private fun durations(selected: Int?, onPick: (Int) -> Unit): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        for (m in Windows.MINUTES_CHOICES) {
            addView(Button(this@SettingsActivity).apply {
                text = Windows.label(m); isAllCaps = false; alpha = if (selected == null || selected == m) 1f else 0.5f
                setOnClickListener { onPick(m) }
            }, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        }
    }

    private fun yn(b: Boolean) = if (b) "on" else "off"

    /** What each paired server says about one capability: "Server: on", or with several connections "Nori: on, Iron: off". */
    private fun serverSays(f: (DeviceSettings) -> String): String {
        val paired = Prefs.pairedSlots(this)
        return when {
            paired.isEmpty() -> "Server: " + f(DeviceSettings())
            paired.size == 1 -> "Server: " + f(Prefs.serverSettings(this, paired[0]))
            else -> paired.joinToString(", ") { Prefs.name(this, it) + ": " + f(Prefs.serverSettings(this, it)) }
        }
    }

    private fun render() {
        box.removeAllViews()
        val slots = Prefs.slots(this)
        val several = slots.size > 1
        val paired = Prefs.anyPaired(this)
        val now = System.currentTimeMillis()
        val agent = Prefs.agent(this)                                  // what the paired server(s) call their assistant ("Iron", "Nori and Iron"), never a fixed "Nori"
        box.addView(title(AgentName.cap(agent) + " on this phone"))
        if (slots.isEmpty()) box.addView(body(StatusLine.build(false, false, false, now, 0, 0, "", "")))
        else slots.forEachIndexed { n, slot ->
            val text = StatusLine.build(Prefs.isPaired(this, slot), Prefs.needsRepair(this, slot), Prefs.paused(this), now, if (n == 0) Prefs.interactUntil(this) else 0L, if (n == 0) Prefs.shareUntil(this) else 0L, Prefs.status(this, slot), Prefs.host(this, slot), Prefs.agent(this, slot))
            box.addView(body(if (several) Prefs.name(this, slot) + ": " + text else text))
        }
        AppUpdater.available(this)?.let { v ->
            box.addView(line("A newer app is ready: $v (this is ${BuildConfig.VERSION_NAME})", true))
            box.addView(button("Update now") { runUpdate() })
        }
        if (paired) {
            box.addView(button("Open $agent (chat and everything else)") { startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP)) })
            box.addView(toggle("Pause everything", Prefs.paused(this)) { Control.setPaused(this, it); render() })
            if (several) box.addView(body("Pause, the switches below and the windows are the phone's own: they apply to every connection."))
        }

        // ── 1. connections ──
        box.addView(head(if (several) "1. Connections" else "1. Pair"))
        for (slot in slots) connectionBlock(slot, several)
        if (slots.size < Connections.MAX) {
            if (slots.isNotEmpty()) {
                box.addView(line("Add another connection", true))
                box.addView(body("Another server (for example a second assistant) can be paired alongside this one. It gets its own tab in the app. Make a pairing code in ITS Settings -> Devices and type its address and the code here, or open its pairing link on this phone."))
            }
            pairForm(newServer = true, initialServer = typedServer)
        }

        // ── 2. what I allow ──
        box.addView(head("2. What I allow"))
        box.addView(body("Every capability needs two keys: the server's switch and yours. Nothing but notifications is on until you turn it on here, and this phone re-checks every request itself." +
            if (several) " Your switch applies to every connection; each server has its own switch, shown below." else ""))
        box.addView(toggle("Notifications (replies, reminders, approvals)", Prefs.ownerNotifications(this)) { Prefs.setOwner(this, "notifications", it); SyncService.pollNow(this); render() })
        box.addView(body(serverSays { yn(it.notifications) } + ". Quiet hours are set in the server's settings." + if (!Notifier.canNotify(this)) " Android notifications are OFF for this app: see Battery & permissions." else ""))
        box.addView(toggle("Share sheet (\"Send to my assistant\")", Prefs.ownerShare(this)) { Prefs.setOwner(this, "share", it); SyncService.pollNow(this) })
        box.addView(body("Text, links and photos you choose to share from any app go to your chat, a note or a task." + (if (several) " The share sheet asks which assistant." else "") + " Nothing is sent unless you share it."))

        box.addView(toggle("Health data (Health Connect)", Prefs.ownerHealth(this)) { on ->
            Prefs.setOwner(this, "health", on)
            if (on && !HealthReader.available(this)) toast("Health Connect is not available on this phone")
            SyncService.requestHealth(this); render()
        })
        box.addView(body(serverSays { s -> yn(s.health) + (if (s.health && s.healthMetrics.isNotEmpty()) " (allows " + s.healthMetrics.joinToString(", ") { HealthMetrics.label(it) } + ")" else "") } +
            ". Read only, every 15 minutes and when you open the app; each reading is sent once" + (if (several) " to each server that allows it" else "") + "."))
        if (Prefs.ownerHealth(this)) {
            val have = try { HealthReader.grantedMetrics(this) } catch (e: Exception) { emptySet() }
            for (m in HealthMetrics.ALL) {
                val on = m in Prefs.healthMetrics(this)
                box.addView(toggle("   " + HealthMetrics.label(m) + (if (on && m !in have) "  (needs Health Connect access)" else ""), on) { v ->
                    Prefs.setHealthMetric(this, m, v)
                    if (v && m !in have && HealthReader.available(this)) { try { startActivityForResult(HealthReader.requestIntent(this, listOf(m)), REQ_HEALTH) } catch (e: Exception) { toast("Couldn't open Health Connect") } }
                    SyncService.requestHealth(this); render()
                })
            }
            if (HealthReader.lastError.isNotEmpty()) box.addView(body("Last problem: " + HealthReader.lastError))
        }

        box.addView(toggle("Places (arrived / left)", Prefs.ownerPlaces(this)) { on ->
            Prefs.setOwner(this, "places", on)
            if (on) askLocation()
            PlaceManager.sync(this); SyncService.pollNow(this); render()
        })
        box.addView(body(serverSays { yn(it.places) } + ". This phone watches your saved places itself and tells $agent only \"arrived\" or \"left\" with the place's name" + (if (several) " (each server hears only about its own places)" else "") +
            ". No coordinates and no location trail are ever sent. It needs location access \"all the time\"."))
        if (Prefs.ownerPlaces(this)) {
            box.addView(line("Location: " + (if (PlaceManager.hasFineLocation(this)) "allowed" else "off") + ", all the time: " + (if (PlaceManager.hasBackgroundLocation(this)) "allowed" else "off"), true))
            if (!PlaceManager.ready(this)) box.addView(button("Allow location") { askLocation() })
        }
        val placeSlots = Prefs.pairedSlots(this)
        if (placeSlots.all { Prefs.serverSettings(this, it).placesList.isEmpty() }) box.addView(body("No places yet.")) else for (slot in placeSlots) {
            for (p in Prefs.serverSettings(this, slot).placesList) {
                val here = Prefs.currentPlace(this, slot) == p.name && Prefs.ownerPlaces(this)
                box.addView(body("• ${p.name}${if (p.home) " (home)" else ""} — ${Geo.effectiveRadius(p.radiusM)} m" + (if (several) "  [${Prefs.name(this, slot)}]" else "") + (if (here) "   ← you are here" else "")))
            }
        }
        box.addView(button("Save where I am now as a place…") { startSavePlace() })

        box.addView(toggle("Phone actions (open links and apps, timers, alarms, directions)", Prefs.ownerActions(this)) { Prefs.setOwner(this, "actions", it); SyncService.pollNow(this); render() })
        box.addView(body(serverSays { yn(it.actions) + ", at most ${it.actionsPerHour} per hour" } + ". Only public web addresses open; apps like Settings, the app store, wallets and password managers are never touched. Android needs \"Display over other apps\" to open things while this app is closed: " + (if (Settings.canDrawOverlays(this)) "allowed." else "NOT allowed yet (see Battery & permissions).")))

        box.addView(toggle("Control (read the screen, tap, type, scroll)", Prefs.ownerControl(this)) { on ->
            Prefs.setOwner(this, "control", on)
            if (!on) WindowControl.closeInteract(this, "switched off")
            SyncService.pollNow(this); render()
        })
        box.addView(body(serverSays { yn(it.control) } + ". Works only while the control window you open below is running, needs the Nori accessibility service (${if (NoriAccessibilityService.isEnabled(this)) "on" else "OFF: see Battery & permissions"}), never reads or types into password, card or code fields, and asks you first (a notification with Approve / Deny, unlock required) before anything like send, pay, delete, install."))

        box.addView(toggle("Screen sharing (one screenshot per request)", Prefs.ownerScreen(this)) { on ->
            Prefs.setOwner(this, "screen", on)
            if (!on) ScreenShareService.stop(this)
            SyncService.pollNow(this); render()
        })
        box.addView(body(serverSays { yn(it.screen) } + ". Works only in the screen-share window you open below. A picture is taken only when $agent asks, is described by a vision model, and is thrown away. Android shows its own capture indicator and this app never hides it. Private and system apps are never captured. This also needs the accessibility service (only so the phone can tell which app is in front)."))

        // ── 3. windows ──
        box.addView(head("3. Windows (you open them, they close themselves)"))
        if (several) box.addView(body("A window is the phone's, not one server's: while it is open, every connection whose server allows it may use it, one request at a time."))
        val iOpen = Prefs.interactOpen(this)
        box.addView(line("Control window: " + if (iOpen) "OPEN, " + Windows.countdown(Windows.remainingMs(Prefs.interactUntil(this), now)) + " left" else "closed", true))
        if (iOpen) box.addView(button("Close the control window now") { WindowControl.closeInteract(this); render() })
        else box.addView(durations(null) { m -> WindowControl.openInteract(this, m)?.let { toast(it) }; render() })
        box.addView(body("The Quick Settings tile \"${AgentName.cap(agent)} may act\" opens it for:"))
        box.addView(durations(Prefs.interactMinutes(this)) { m -> Prefs.setInteractMinutes(this, m); render() })
        val sOpen = Prefs.shareOpen(this)
        box.addView(line("Screen-share window: " + if (sOpen) "OPEN, " + Windows.countdown(Windows.remainingMs(Prefs.shareUntil(this), now)) + " left" else "closed", true))
        if (sOpen) box.addView(button("Stop screen sharing now") { ScreenShareService.stop(this); later { render() } })
        else box.addView(durations(null) { m ->
            val problem = WindowControl.shareBlocker(this)
            if (problem != null) toast(problem) else startActivity(Intent(this, ScreenShareRequestActivity::class.java).putExtra(ScreenShareRequestActivity.EXTRA_MINUTES, m))
        })
        box.addView(body("The Quick Settings tile \"${AgentName.cap(agent)} may look\" opens it for:"))
        box.addView(durations(Prefs.shareMinutes(this)) { m -> Prefs.setShareMinutes(this, m); render() })
        box.addView(toggle("Keep the screen on while a window is open", Prefs.keepAwake(this)) { Prefs.setKeepAwake(this, it) })

        // ── 4. sent log ──
        box.addView(head("4. Everything this app has sent"))
        val log = SentLog.recent(this, 40)
        box.addView(TextView(this).apply {
            text = if (log.isEmpty()) "Nothing yet." else log.joinToString("\n")
            textSize = 12f; setTextColor(dim); typeface = Typeface.MONOSPACE
        })
        NoriApp.lastCrash(this)?.let { crash ->
            box.addView(line("The app crashed last time:", true))
            box.addView(TextView(this).apply { text = crash.lineSequence().take(8).joinToString("\n"); textSize = 11f; setTextColor(dim); typeface = Typeface.MONOSPACE })
        }
        box.addView(button("Copy the log (and last crash)") {
            val text = "Nori app ${BuildConfig.VERSION_NAME}\n" + SentLog.recent(this, 120).reversed().joinToString("\n") + (NoriApp.lastCrash(this)?.let { "\n\n--- last crash ---\n$it" } ?: "")
            (getSystemService(CLIPBOARD_SERVICE) as android.content.ClipboardManager).setPrimaryClip(android.content.ClipData.newPlainText("Nori app log", text))
            toast("Copied: paste it wherever you want to send it")
        })
        box.addView(button("Refresh") { render() })
        box.addView(button("Clear this list") { SentLog.clear(this); NoriApp.clearCrash(this); render() })

        // ── 5. battery & permissions ──
        box.addView(head("5. Battery & permissions help"))
        val canPost = Notifier.canNotify(this)
        box.addView(line("Notifications: " + if (canPost) "allowed" else "OFF", true))
        if (!canPost) {
            if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) box.addView(button("Allow notifications") { requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQ_NOTIF) })
            box.addView(button("Open Android's notification settings for this app") { startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, packageName)) })
        }
        val batteryOk = (getSystemService(POWER_SERVICE) as PowerManager).isIgnoringBatteryOptimizations(packageName)
        box.addView(line("Stay connected in the background: " + if (batteryOk) "allowed" else "off", true))
        box.addView(body("Without this Android may put the connection to sleep and replies and reminders arrive late."))
        if (!batteryOk) box.addView(button("Allow background activity") { startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).setData(Uri.parse("package:$packageName"))) })
        box.addView(line("Display over other apps: " + if (Settings.canDrawOverlays(this)) "allowed" else "off", true))
        box.addView(body("Used for two things only: letting $agent open a link or app while this app is closed (Android blocks that otherwise), and keeping the screen on while a window is open. Not needed if you do not use phone actions or windows."))
        if (!Settings.canDrawOverlays(this)) box.addView(button("Allow \"Display over other apps\"") { startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName"))) })
        val a11y = NoriAccessibilityService.isEnabled(this)
        box.addView(line("Accessibility service: " + if (a11y) "on" else "off", true))
        box.addView(body("Needed for control and for screen sharing. It does nothing between requests: it watches nothing and stores nothing. Android shows a strong warning when you turn it on; that is how it describes this kind of permission."))
        box.addView(button(if (a11y) "Accessibility settings" else "Turn on the accessibility service") { startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) })
        box.addView(line("If Android says \"Restricted setting\" (Android 13 and newer)", true))
        box.addView(body("Because this app was installed from a file, not a store, Android blocks its accessibility switch at first. To allow it:\n1. Press \"App info\" below (or: Settings -> Apps -> Nori).\n2. Tap the three-dot menu in the top right corner.\n3. Choose \"Allow restricted settings\" and confirm with your PIN or fingerprint.\n4. Go back and turn the Nori accessibility service on.\nIf the menu is missing, first try to turn the service on once (it will be refused), then look again."))
        box.addView(button("App info (for \"Allow restricted settings\")") { startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName"))) })
        if (!packageManager.canRequestPackageInstalls()) box.addView(button("Allow this app to install its own updates") { startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:$packageName"))) })

        // ── 6. version & update ──
        box.addView(head("6. Version & update"))
        box.addView(line("Nori app ${BuildConfig.VERSION_NAME} (build ${BuildConfig.VERSION_CODE})"))
        val latest = AppUpdater.latest(this)
        box.addView(body(if (latest.isEmpty()) "The server has not told this phone about a newer version." else if (AppUpdater.available(this) != null) "Version $latest is ready on your server." else "You have the newest version ($latest)."))
        box.addView(button("Check now") { SyncService.pollNow(this); later(1500) { render() } })
        if (AppUpdater.available(this) != null) box.addView(button("Download and install $latest") { runUpdate() })
    }

    /** One connection: who it is, and what can be done with it (rename, unpair; or pair again, remove). */
    private fun connectionBlock(slot: Int, several: Boolean) {
        val name = Prefs.name(this, slot)
        val host = Prefs.host(this, slot)
        val isPaired = Prefs.isPaired(this, slot)
        if (isPaired && !Prefs.needsRepair(this, slot)) {
            box.addView(line((if (several) "$name: " else "") + "Paired as \"${Prefs.deviceName(this, slot)}\" with $host", true))
            if (!several) box.addView(body("The website's own login is used inside the app; the app never sees your password. A paired phone gets notifications and nothing else until you switch more on below."))
            if (several) box.addView(button("Rename \"$name\" (the tab's title)") { renameDialog(slot) })
            box.addView(button(if (several) "Unpair $name" else "Unpair this phone") {
                AlertDialog.Builder(this).setTitle(if (several) "Unpair from $name?" else "Unpair this phone?").setMessage("The server stops sending to it and it stops listening. You can pair again with a new code.")
                    .setNegativeButton("Cancel", null).setPositiveButton("Unpair") { _, _ ->
                        Thread {
                            Api.post(this, slot, "/device/unregister", JSONObject(), 10_000)
                            runOnUiThread {
                                if (Prefs.pairedSlots(this).size <= 1) { WindowControl.closeInteract(this, "unpaired"); ScreenShareService.stop(this) }   // the windows are the phone's: they stay while another server is paired
                                Prefs.unpair(this, slot); PlaceManager.sync(this)
                                if (!Prefs.anyPaired(this)) SyncService.stop(this)
                                SentLog.add(this, "unpaired" + (if (several) " from $name" else "")); render()
                            }
                        }.start()
                    }.show()
            })
        } else {
            box.addView(line((if (several) "$name: " else "") + (if (isPaired) "needs pairing again" else "not paired") + " ($host)", true))
            pairForm(newServer = false, initialServer = Prefs.server(this, slot))
            box.addView(button("Remove this connection") {
                AlertDialog.Builder(this).setTitle("Remove $name?").setMessage("The app forgets this server's address. Pair again any time with a new code.")
                    .setNegativeButton("Cancel", null).setPositiveButton("Remove") { _, _ ->
                        Prefs.removeConnection(this, slot); PlaceManager.sync(this); SyncService.refresh(this)
                        if (!Prefs.anyPaired(this)) SyncService.stop(this)
                        SentLog.add(this, "removed the connection to $host"); render()
                    }.show()
            })
        }
    }

    /** Server address, code, Pair. For a connection that needs pairing the address is its own; for a new one it is typed. */
    private fun pairForm(newServer: Boolean, initialServer: String) {
        val server = field("Server address, e.g. https://nori.example.com", initialServer)
        box.addView(server)
        box.addView(body("On your server: Settings -> Devices -> \"Pair a phone\" shows a one-time code (valid 15 minutes) and a link that opens this app and pairs in one tap. Or type the code here. HTTPS only."))
        val code = field("pairing code (XXXX-XXXX)", "", code = true)
        box.addView(code)
        box.addView(button("Pair") {
            if (newServer) typedServer = server.text.toString()
            PairHelper.pair(this, server.text.toString(), code.text.toString()) { ok, msg, slot ->
                toast(msg)
                if (ok) { typedServer = ""; Prefs.setSelected(this, slot) }      // the app opens on the connection that was just paired
                render()
            }
        })
    }

    private fun renameDialog(slot: Int) {
        val input = EditText(this).apply { setText(Prefs.label(this@SettingsActivity, slot)); isSingleLine = true; setSelectAllOnFocus(true) }
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(20), dp(8), dp(20), 0); addView(input) }
        AlertDialog.Builder(this).setTitle("Name this connection").setMessage("Shown on its tab and wherever the app says which assistant it means.").setView(col)
            .setNegativeButton("Cancel", null).setPositiveButton("Save") { _, _ -> Prefs.setLabel(this, slot, input.text.toString()); render() }.show()
    }

    private fun runUpdate() {
        toast("Downloading the update…")
        Thread {
            val err = AppUpdater.downloadAndInstall(this)
            runOnUiThread { if (err != null) toast(err) }
        }.start()
    }

    private fun askLocation() {
        if (!PlaceManager.hasFineLocation(this)) requestPermissions(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION), REQ_LOC)
        else if (!PlaceManager.hasBackgroundLocation(this)) requestPermissions(arrayOf(Manifest.permission.ACCESS_BACKGROUND_LOCATION), REQ_BGLOC)
    }

    /** "Save where I am now": which server gets the place is asked first when there is more than one. */
    private fun startSavePlace() {
        val usable = Prefs.usableSlots(this)
        when {
            usable.isEmpty() -> toast("Pair this phone with a Nori server first")
            usable.size == 1 -> startSavePlace(usable[0])
            else -> AlertDialog.Builder(this).setTitle("Save the place for which assistant?").setItems(usable.map { Prefs.name(this, it) }.toTypedArray()) { _, i -> startSavePlace(usable[i]) }.show()
        }
    }

    /** "Save where I am now": the ONE time coordinates leave the phone (a single read, sent with the name, to POST /device/place of the chosen server). */
    private fun startSavePlace(slot: Int) {
        saveSlot = slot
        if (!PlaceManager.hasFineLocation(this)) { requestPermissions(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION), REQ_LOC_SAVE); return }
        val name = EditText(this).apply { hint = "Name (Home, Work, Gym...)"; isSingleLine = true }
        val home = CheckBox(this).apply { text = "This is home" }
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(20), dp(8), dp(20), 0); addView(name); addView(home) }
        val to = if (Prefs.slots(this).size > 1) " It is saved on ${Prefs.name(this, slot)} only." else ""
        AlertDialog.Builder(this).setTitle("Save this place").setMessage("Your location is read once and sent to ${Prefs.agent(this, slot)} with this name. After that only \"arrived\" and \"left\" are ever reported.$to").setView(col)
            .setNegativeButton("Cancel", null).setPositiveButton("Save") { _, _ ->
                val n = name.text.toString().trim()
                if (n.isEmpty()) { toast("Give the place a name"); return@setPositiveButton }
                try {
                    LocationServices.getFusedLocationProviderClient(this).getCurrentLocation(Priority.PRIORITY_HIGH_ACCURACY, null).addOnSuccessListener { l ->
                        if (l == null || !Geo.validLatLon(l.latitude, l.longitude)) { toast("Couldn't get your location. Try again outside."); return@addOnSuccessListener }
                        val near = Geo.nearestWithin(l.latitude, l.longitude, Prefs.serverSettings(this, slot).placesList, 60.0)
                        if (near != null) {
                            AlertDialog.Builder(this).setTitle("Already saved?").setMessage("\"${near.name}\" is already about here. Save \"$n\" anyway?").setNegativeButton("No", null)
                                .setPositiveButton("Save anyway") { _, _ -> sendPlace(slot, n, home.isChecked, l.latitude, l.longitude) }.show()
                        } else sendPlace(slot, n, home.isChecked, l.latitude, l.longitude)
                    }.addOnFailureListener { toast("Couldn't get your location") }
                } catch (e: SecurityException) { toast("Location is not allowed for this app") }
            }.show()
    }

    private fun sendPlace(slot: Int, name: String, home: Boolean, lat: Double, lon: Double) {
        Thread {
            val r = Api.post(this, slot, "/device/place", Protocol.place(name, lat, lon, null, home))
            val who = if (Prefs.slots(this).size > 1) " on ${Prefs.name(this, slot)}" else ""
            SentLog.add(this, if (r.ok) "saved a place$who: $name (coordinates sent this once)" else "could not save the place$who: ${r.error}")
            runOnUiThread { toast(if (r.ok) "Saved \"$name\"" else "Couldn't save: ${r.error}"); SyncService.pollNow(this); later(1500) { render() } }
        }.start()
    }

    @Deprecated("the classic result API keeps this screen simple")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQ_HEALTH) { SyncService.requestHealth(this); later(400) { render() } }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        val ok = grantResults.any { it == PackageManager.PERMISSION_GRANTED }
        when (requestCode) {
            REQ_NOTIF -> { Notifier.refreshSync(this); render() }
            REQ_LOC -> { if (ok && !PlaceManager.hasBackgroundLocation(this)) requestPermissions(arrayOf(Manifest.permission.ACCESS_BACKGROUND_LOCATION), REQ_BGLOC); PlaceManager.sync(this); render() }
            REQ_BGLOC -> { PlaceManager.sync(this); render() }
            REQ_LOC_SAVE -> if (ok) startSavePlace(saveSlot)
        }
    }
}
