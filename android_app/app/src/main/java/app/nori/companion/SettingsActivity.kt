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
import app.nori.companion.core.Geo
import app.nori.companion.core.HealthMetrics
import app.nori.companion.core.Protocol
import app.nori.companion.core.ServerUrl
import app.nori.companion.core.StatusLine
import app.nori.companion.core.Windows
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import org.json.JSONObject

/**
 * Everything about what this phone lets Nori do, in one place: pairing, one switch per capability (each is the phone's own half; the server has its own), the two windows you open
 * yourself, the list of everything this app sent, and help with Android's permissions. Nothing beyond notifications is on until you turn it on here.
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
        typedServer = Prefs.server(this)
    }

    override fun onResume() {
        super.onResume()
        render()
        if (Prefs.isPaired(this)) SyncService.start(this)
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

    private fun render() {
        box.removeAllViews()
        val paired = Prefs.isPaired(this)
        val s = Prefs.serverSettings(this)
        val now = System.currentTimeMillis()
        box.addView(title("Nori on this phone"))
        box.addView(body(StatusLine.build(paired, Prefs.needsRepair(this), Prefs.paused(this), now, Prefs.interactUntil(this), Prefs.shareUntil(this), Prefs.status(this), ServerUrl.host(Prefs.server(this)))))
        AppUpdater.available(this)?.let { v ->
            box.addView(line("A newer app is ready: $v (this is ${BuildConfig.VERSION_NAME})", true))
            box.addView(button("Update now") { runUpdate() })
        }
        if (paired) {
            box.addView(button("Open Nori (chat and everything else)") { startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP)) })
            box.addView(toggle("Pause everything", Prefs.paused(this)) { Control.setPaused(this, it); render() })
        }

        // ── 1. pair ──
        box.addView(head("1. Pair"))
        if (paired && !Prefs.needsRepair(this)) {
            box.addView(line("Paired as \"${Prefs.deviceName(this)}\" with ${ServerUrl.host(Prefs.server(this))}", true))
            box.addView(body("The website's own login is used inside the app; the app never sees your password. A paired phone gets notifications and nothing else until you switch more on below."))
            box.addView(button("Unpair this phone") {
                AlertDialog.Builder(this).setTitle("Unpair this phone?").setMessage("Nori stops sending to it and it stops listening. You can pair again with a new code.")
                    .setNegativeButton("Cancel", null).setPositiveButton("Unpair") { _, _ ->
                        Thread {
                            Api.post(this, "/device/unregister", JSONObject(), 10_000)
                            runOnUiThread {
                                WindowControl.closeInteract(this, "unpaired"); ScreenShareService.stop(this)
                                Prefs.unpair(this); PlaceManager.sync(this); SyncService.stop(this); SentLog.add(this, "unpaired"); render()
                            }
                        }.start()
                    }.show()
            })
        } else {
            val server = field("Server address, e.g. https://nori.example.com", typedServer)
            box.addView(server)
            box.addView(body("In Nori: Settings -> Devices -> \"Pair a phone\" shows a one-time code (valid 15 minutes) and a link that opens this app and pairs in one tap. Or type the code here. HTTPS only."))
            val code = field("pairing code (XXXX-XXXX)", "", code = true)
            box.addView(code)
            box.addView(button("Pair") {
                typedServer = server.text.toString()
                PairHelper.pair(this, typedServer, code.text.toString()) { ok, msg -> toast(msg); if (ok) typedServer = Prefs.server(this); render() }
            })
        }

        // ── 2. what I allow ──
        box.addView(head("2. What I allow"))
        box.addView(body("Every capability needs two keys: the server's switch and yours. Nothing but notifications is on until you turn it on here, and this phone re-checks every request itself."))
        box.addView(toggle("Notifications (replies, reminders, approvals)", Prefs.ownerNotifications(this)) { Prefs.setOwner(this, "notifications", it); SyncService.pollNow(this); render() })
        box.addView(body("Server: ${yn(s.notifications)}. Quiet hours are set in Nori's settings." + if (!Notifier.canNotify(this)) " Android notifications are OFF for this app: see Battery & permissions." else ""))
        box.addView(toggle("Share sheet (\"Send to Nori\")", Prefs.ownerShare(this)) { Prefs.setOwner(this, "share", it); SyncService.pollNow(this) })
        box.addView(body("Text, links and photos you choose to share from any app go to your chat, a note or a task. Nothing is sent unless you share it."))

        box.addView(toggle("Health data (Health Connect)", Prefs.ownerHealth(this)) { on ->
            Prefs.setOwner(this, "health", on)
            if (on && !HealthReader.available(this)) toast("Health Connect is not available on this phone")
            SyncService.requestHealth(this); render()
        })
        box.addView(body("Server: ${yn(s.health)}${if (s.health && s.healthMetrics.isNotEmpty()) " (allows " + s.healthMetrics.joinToString(", ") { HealthMetrics.label(it) } + ")" else ""}. Read only, every 15 minutes and when you open the app; each reading is sent once."))
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
        box.addView(body("Server: ${yn(s.places)}. This phone watches your saved places itself and tells Nori only \"arrived\" or \"left\" with the place's name. No coordinates and no location trail are ever sent. It needs location access \"all the time\"."))
        if (Prefs.ownerPlaces(this)) {
            box.addView(line("Location: " + (if (PlaceManager.hasFineLocation(this)) "allowed" else "off") + ", all the time: " + (if (PlaceManager.hasBackgroundLocation(this)) "allowed" else "off"), true))
            if (!PlaceManager.ready(this)) box.addView(button("Allow location") { askLocation() })
        }
        if (s.placesList.isEmpty()) box.addView(body("No places yet.")) else for (p in s.placesList) {
            box.addView(body("• ${p.name}${if (p.home) " (home)" else ""} — ${Geo.effectiveRadius(p.radiusM)} m" + (if (Prefs.currentPlace(this) == p.name && Prefs.ownerPlaces(this)) "   ← you are here" else "")))
        }
        box.addView(button("Save where I am now as a place…") { startSavePlace() })

        box.addView(toggle("Phone actions (open links and apps, timers, alarms, directions)", Prefs.ownerActions(this)) { Prefs.setOwner(this, "actions", it); SyncService.pollNow(this); render() })
        box.addView(body("Server: ${yn(s.actions)}, at most ${s.actionsPerHour} per hour. Only public web addresses open; apps like Settings, the app store, wallets and password managers are never touched. Android needs \"Display over other apps\" to open things while Nori is closed: " + (if (Settings.canDrawOverlays(this)) "allowed." else "NOT allowed yet (see Battery & permissions).")))

        box.addView(toggle("Control (read the screen, tap, type, scroll)", Prefs.ownerControl(this)) { on ->
            Prefs.setOwner(this, "control", on)
            if (!on) WindowControl.closeInteract(this, "switched off")
            SyncService.pollNow(this); render()
        })
        box.addView(body("Server: ${yn(s.control)}. Works only while the control window you open below is running, needs the Nori accessibility service (${if (NoriAccessibilityService.isEnabled(this)) "on" else "OFF: see Battery & permissions"}), never reads or types into password, card or code fields, and asks you first (a notification with Approve / Deny, unlock required) before anything like send, pay, delete, install."))

        box.addView(toggle("Screen sharing (one screenshot per request)", Prefs.ownerScreen(this)) { on ->
            Prefs.setOwner(this, "screen", on)
            if (!on) ScreenShareService.stop(this)
            SyncService.pollNow(this); render()
        })
        box.addView(body("Server: ${yn(s.screen)}. Works only in the screen-share window you open below. A picture is taken only when Nori asks, is described by a vision model, and is thrown away. Android shows its own capture indicator and this app never hides it. Private and system apps are never captured. This also needs the accessibility service (only so the phone can tell which app is in front)."))

        // ── 3. windows ──
        box.addView(head("3. Windows (you open them, they close themselves)"))
        val iOpen = Prefs.interactOpen(this)
        box.addView(line("Control window: " + if (iOpen) "OPEN, " + Windows.countdown(Windows.remainingMs(Prefs.interactUntil(this), now)) + " left" else "closed", true))
        if (iOpen) box.addView(button("Close the control window now") { WindowControl.closeInteract(this); render() })
        else box.addView(durations(null) { m -> WindowControl.openInteract(this, m)?.let { toast(it) }; render() })
        box.addView(body("The Quick Settings tile \"Nori may act\" opens it for:"))
        box.addView(durations(Prefs.interactMinutes(this)) { m -> Prefs.setInteractMinutes(this, m); render() })
        val sOpen = Prefs.shareOpen(this)
        box.addView(line("Screen-share window: " + if (sOpen) "OPEN, " + Windows.countdown(Windows.remainingMs(Prefs.shareUntil(this), now)) + " left" else "closed", true))
        if (sOpen) box.addView(button("Stop screen sharing now") { ScreenShareService.stop(this); later { render() } })
        else box.addView(durations(null) { m ->
            val problem = WindowControl.shareBlocker(this)
            if (problem != null) toast(problem) else startActivity(Intent(this, ScreenShareRequestActivity::class.java).putExtra(ScreenShareRequestActivity.EXTRA_MINUTES, m))
        })
        box.addView(body("The Quick Settings tile \"Nori may look\" opens it for:"))
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
            box.addView(button("Open Android's notification settings for Nori") { startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, packageName)) })
        }
        val batteryOk = (getSystemService(POWER_SERVICE) as PowerManager).isIgnoringBatteryOptimizations(packageName)
        box.addView(line("Stay connected in the background: " + if (batteryOk) "allowed" else "off", true))
        box.addView(body("Without this Android may put the connection to sleep and replies and reminders arrive late."))
        if (!batteryOk) box.addView(button("Allow background activity") { startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).setData(Uri.parse("package:$packageName"))) })
        box.addView(line("Display over other apps: " + if (Settings.canDrawOverlays(this)) "allowed" else "off", true))
        box.addView(body("Used for two things only: letting Nori open a link or app while this app is closed (Android blocks that otherwise), and keeping the screen on while a window is open. Not needed if you do not use phone actions or windows."))
        if (!Settings.canDrawOverlays(this)) box.addView(button("Allow \"Display over other apps\"") { startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName"))) })
        val a11y = NoriAccessibilityService.isEnabled(this)
        box.addView(line("Accessibility service: " + if (a11y) "on" else "off", true))
        box.addView(body("Needed for control and for screen sharing. It does nothing between requests: it watches nothing and stores nothing. Android shows a strong warning when you turn it on; that is how it describes this kind of permission."))
        box.addView(button(if (a11y) "Accessibility settings" else "Turn on the accessibility service") { startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) })
        box.addView(line("If Android says \"Restricted setting\" (Android 13 and newer)", true))
        box.addView(body("Because this app was installed from a file, not a store, Android blocks its accessibility switch at first. To allow it:\n1. Press \"App info\" below (or: Settings -> Apps -> Nori).\n2. Tap the three-dot menu in the top right corner.\n3. Choose \"Allow restricted settings\" and confirm with your PIN or fingerprint.\n4. Go back and turn the Nori accessibility service on.\nIf the menu is missing, first try to turn the service on once (it will be refused), then look again."))
        box.addView(button("App info (for \"Allow restricted settings\")") { startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName"))) })
        if (!packageManager.canRequestPackageInstalls()) box.addView(button("Allow Nori to install its own updates") { startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:$packageName"))) })

        // ── 6. version & update ──
        box.addView(head("6. Version & update"))
        box.addView(line("Nori app ${BuildConfig.VERSION_NAME} (build ${BuildConfig.VERSION_CODE})"))
        val latest = Prefs.latestApp(this)
        box.addView(body(if (latest.isEmpty()) "The server has not told this phone about a newer version." else if (AppUpdater.available(this) != null) "Version $latest is ready on your server." else "You have the newest version ($latest)."))
        box.addView(button("Check now") { SyncService.pollNow(this); later(1500) { render() } })
        if (AppUpdater.available(this) != null) box.addView(button("Download and install $latest") { runUpdate() })
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

    /** "Save where I am now": the ONE time coordinates leave the phone (a single read, sent with the name, to POST /device/place). */
    private fun startSavePlace() {
        if (!Prefs.isPaired(this)) { toast("Pair with Nori first"); return }
        if (!PlaceManager.hasFineLocation(this)) { requestPermissions(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION), REQ_LOC_SAVE); return }
        val name = EditText(this).apply { hint = "Name (Home, Work, Gym...)"; isSingleLine = true }
        val home = CheckBox(this).apply { text = "This is home" }
        val col = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(20), dp(8), dp(20), 0); addView(name); addView(home) }
        AlertDialog.Builder(this).setTitle("Save this place").setMessage("Your location is read once and sent to Nori with this name. After that only \"arrived\" and \"left\" are ever reported.").setView(col)
            .setNegativeButton("Cancel", null).setPositiveButton("Save") { _, _ ->
                val n = name.text.toString().trim()
                if (n.isEmpty()) { toast("Give the place a name"); return@setPositiveButton }
                try {
                    LocationServices.getFusedLocationProviderClient(this).getCurrentLocation(Priority.PRIORITY_HIGH_ACCURACY, null).addOnSuccessListener { l ->
                        if (l == null || !Geo.validLatLon(l.latitude, l.longitude)) { toast("Couldn't get your location. Try again outside."); return@addOnSuccessListener }
                        val near = Geo.nearestWithin(l.latitude, l.longitude, Prefs.serverSettings(this).placesList, 60.0)
                        if (near != null) {
                            AlertDialog.Builder(this).setTitle("Already saved?").setMessage("\"${near.name}\" is already about here. Save \"$n\" anyway?").setNegativeButton("No", null)
                                .setPositiveButton("Save anyway") { _, _ -> sendPlace(n, home.isChecked, l.latitude, l.longitude) }.show()
                        } else sendPlace(n, home.isChecked, l.latitude, l.longitude)
                    }.addOnFailureListener { toast("Couldn't get your location") }
                } catch (e: SecurityException) { toast("Location is not allowed for this app") }
            }.show()
    }

    private fun sendPlace(name: String, home: Boolean, lat: Double, lon: Double) {
        Thread {
            val r = Api.post(this, "/device/place", Protocol.place(name, lat, lon, null, home))
            SentLog.add(this, if (r.ok) "saved a place: $name (coordinates sent this once)" else "could not save the place: ${r.error}")
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
            REQ_LOC_SAVE -> if (ok) startSavePlace()
        }
    }
}
