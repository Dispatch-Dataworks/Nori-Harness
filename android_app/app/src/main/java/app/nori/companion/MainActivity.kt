package app.nori.companion

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.app.DownloadManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.PowerManager
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.PermissionRequest
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import app.nori.companion.core.Connections
import app.nori.companion.core.Deeplink
import app.nori.companion.core.Link
import app.nori.companion.core.ServerUrl

/**
 * Nori in a full-screen web view: the chat, the settings, every page, signed in the way the website is (the website's own login; this app never sees the password).
 * Each paired server (a "connection") has its own web view, shown as a tab along the top once there is more than one; with one there is no bar and it looks as it always did.
 * A web view only loads its own server's pages; links to other sites open in the phone's browser. Notifications, widgets and shortcuts open the chat of the connection they belong to.
 */
class MainActivity : Activity() {
    private class Tab(val slot: Int, val web: WebView) { var loadedServer = "" }

    private val tabs = LinkedHashMap<Int, Tab>()
    private lateinit var bar: LinearLayout
    private lateinit var holder: FrameLayout
    private var current = 0
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private var pendingPermission: PermissionRequest? = null
    private val REQ_FILE = 71
    private val REQ_MIC = 72
    private val REQ_NOTIF = 74
    private var pendingPath: String? = null
    private var pendingSlot: Int? = null

    private val barBg = Color.parseColor("#1A1A1A")
    private val dim = Color.parseColor("#9AA0A6")
    private val accent = Color.parseColor("#7FD1AE")

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        window.setBackgroundDrawable(android.graphics.drawable.ColorDrawable(Color.parseColor("#111111")))
        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        bar = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; setBackgroundColor(barBg); visibility = View.GONE }
        holder = FrameLayout(this)
        root.addView(bar, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        root.addView(holder, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f))
        setContentView(root)
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, wi ->
            val i = wi.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime())
            v.setPadding(i.left, i.top, i.right, i.bottom)
            WindowInsetsCompat.CONSUMED
        }
        current = Prefs.selected(this)
        if (Build.VERSION.SDK_INT >= 33) {
            onBackInvokedDispatcher.registerOnBackInvokedCallback(android.window.OnBackInvokedDispatcher.PRIORITY_DEFAULT) { goBackOrLeave() }
        }
        Notifier.channels(this)
        if (Build.VERSION.SDK_INT >= 33 && !Prefs.flag(this, "asked_notifications") && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            Prefs.setFlag(this, "asked_notifications")
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQ_NOTIF)
        }
        handleIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIntent(intent)
        if (pendingPath != null) loadPending()
    }

    private fun handleIntent(i: Intent?) {
        when (val link = Deeplink.parse(i?.dataString)) {
            is Link.Pair -> confirmPair(link)
            Link.Settings -> startActivity(Intent(this, SettingsActivity::class.java))
            Link.AddTask -> startActivity(Intent(this, QuickTaskActivity::class.java))
            Link.Chat -> { pendingPath = Deeplink.CHAT_PATH; pendingSlot = Deeplink.connOf(i?.dataString) }
            Link.Talk -> { pendingPath = Deeplink.TALK_PATH; pendingSlot = Deeplink.connOf(i?.dataString) }
            is Link.Prefill -> { pendingPath = Deeplink.prefillPath(link.text); pendingSlot = Deeplink.connOf(i?.dataString) }
            Link.Unknown -> {}
        }
        i?.data = null           // handled: do not run it again if the activity is recreated
    }

    /** A pairing link can come from anywhere, so the owner confirms which server before anything is sent to it. */
    private fun confirmPair(link: Link.Pair) {
        val server = link.server ?: Prefs.server(this, current).ifEmpty { null }
        if (server == null) { Toast.makeText(this, "That pairing link has no server address.", Toast.LENGTH_LONG).show(); startActivity(Intent(this, SettingsActivity::class.java)); return }
        val host = ServerUrl.host(server)
        val target = PairHelper.target(this, server)
        if (target == null || target.kind == Connections.Kind.FULL) {
            Toast.makeText(this, "This phone already has ${Connections.MAX} connections. Remove one in the app settings first.", Toast.LENGTH_LONG).show()
            startActivity(Intent(this, SettingsActivity::class.java))
            return
        }
        val what = when (target.kind) {
            Connections.Kind.NEW -> if (Prefs.slots(this).isEmpty()) "" else "\n\nThis adds $host as another connection: the ones this phone already has stay as they are."
            Connections.Kind.SAME_SERVER -> "\n\nThis phone is already connected to $host: it is paired again and the old pairing is replaced."
            else -> ""
        }
        AlertDialog.Builder(this)
            .setTitle("Pair this phone with $host?")
            .setMessage("Only continue if you made this pairing code yourself in your server's Settings -> Devices. Pairing lets that server send you notifications; anything more is a separate switch you turn on here.$what")
            .setNegativeButton("Cancel", null)
            .setPositiveButton("Pair") { _, _ ->
                PairHelper.pair(this, server, link.code) { ok, msg, slot ->
                    Toast.makeText(this, msg, Toast.LENGTH_LONG).show()
                    if (ok) {
                        current = slot
                        Prefs.setSelected(this, slot)
                        tabs[slot]?.loadedServer = ""
                        onResume()
                    } else startActivity(Intent(this, SettingsActivity::class.java))
                }
            }.show()
    }

    // ── tabs ─────────────────────────────────────────────────────────
    /** One web view per connection the phone has; none for one that was removed. The bar shows only when there is more than one. */
    private fun syncTabs() {
        val slots = Prefs.slots(this)
        for ((s, t) in tabs.entries.toList()) if (s !in slots) { holder.removeView(t.web); t.web.destroy(); tabs.remove(s) }
        for (s in slots) if (s !in tabs) {
            val t = Tab(s, WebView(this))
            configureWebView(t)
            holder.addView(t.web, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
            tabs[s] = t
        }
        current = Connections.pick(null, slots, current)
        for ((s, t) in tabs) t.web.visibility = if (s == current) View.VISIBLE else View.GONE
        AppState.visibleSlot = if (tabs.isEmpty()) -1 else current
        renderBar()
    }

    private fun select(slot: Int) {
        if (slot == current || slot !in tabs) return
        current = slot
        Prefs.setSelected(this, slot)
        for ((s, t) in tabs) t.web.visibility = if (s == slot) View.VISIBLE else View.GONE
        AppState.visibleSlot = slot
        ensureLoaded(tabs[slot])
        renderBar()
        SyncService.pollNow(this)                                 // each server learns at once whether its chat is the one in front
    }

    private fun ensureLoaded(t: Tab?) {
        if (t == null) return
        val server = Prefs.server(this, t.slot)
        if (server.isNotEmpty() && t.loadedServer != server) { t.loadedServer = server; t.web.loadUrl(ServerUrl.page(server, "/")) }
    }

    private fun renderBar() {
        bar.removeAllViews()
        if (tabs.size < 2) { bar.visibility = View.GONE; return }
        bar.visibility = View.VISIBLE
        val names = Prefs.names(this)
        for (s in tabs.keys) {
            val on = s == current
            bar.addView(TextView(this).apply {
                text = names[s] ?: Prefs.label(this@MainActivity, s)
                textSize = 15f; gravity = Gravity.CENTER; maxLines = 1; ellipsize = android.text.TextUtils.TruncateAt.END
                setTextColor(if (on) accent else dim); setTypeface(null, if (on) Typeface.BOLD else Typeface.NORMAL)
                setBackgroundColor(if (on) Color.parseColor("#262626") else barBg)
                setPadding(dp(8), 0, dp(8), 0)
                setOnClickListener { select(s) }
            }, LinearLayout.LayoutParams(0, dp(44), 1f))
        }
        bar.addView(TextView(this).apply {
            text = "⚙"; textSize = 18f; gravity = Gravity.CENTER; setTextColor(dim)
            contentDescription = "App settings"
            setOnClickListener { startActivity(Intent(this@MainActivity, SettingsActivity::class.java)) }
        }, LinearLayout.LayoutParams(dp(52), dp(44)))
    }

    override fun onResume() {
        super.onResume()
        AppState.appVisible = true
        syncTabs()
        SyncService.pollNow(this)                                // tell the server the chat is in front, so it holds notifications back
        val slots = Prefs.slots(this)
        if (!Prefs.anyPaired(this)) {
            if (!Prefs.flag(this, "welcomed")) { Prefs.setFlag(this, "welcomed"); startActivity(Intent(this, SettingsActivity::class.java)) }
            else if (slots.isEmpty()) startActivity(Intent(this, SettingsActivity::class.java))
        }
        if (slots.isNotEmpty()) {
            if (pendingPath != null) loadPending() else ensureLoaded(tabs[current])
        }
        if (Prefs.anyPaired(this)) {
            SyncService.start(this)
            SyncService.refresh(this)
            SyncService.requestHealth(this)
            askBatteryOnce()
        }
    }

    private fun loadPending() {
        syncTabs()
        val p = pendingPath ?: return
        val wanted = pendingSlot
        pendingPath = null
        pendingSlot = null
        val slot = Connections.pick(wanted, Prefs.slots(this), current)
        val tab = tabs[slot] ?: return
        val server = Prefs.server(this, slot)
        if (server.isEmpty()) return
        if (slot != current) select(slot)
        tab.loadedServer = server
        tab.web.loadUrl(ServerUrl.page(server, p))
    }

    /** Once, right after pairing: ask Android not to put the connection to sleep (replies and reminders would otherwise arrive late). */
    private fun askBatteryOnce() {
        if (Prefs.flag(this, "asked_battery")) return
        Prefs.setFlag(this, "asked_battery")
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        if (!pm.isIgnoringBatteryOptimizations(packageName)) {
            try { startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).setData(Uri.parse("package:$packageName"))) } catch (e: Exception) { /* the settings screen has a button for it */ }
        }
    }

    override fun onPause() {
        AppState.appVisible = false
        SyncService.pollNow(this)
        CookieManager.getInstance().flush()
        super.onPause()
    }

    @Deprecated("kept for older Android versions; newer ones use the back-invoked callback")
    override fun onBackPressed() { goBackOrLeave() }

    private fun goBackOrLeave() {
        val web = tabs[current]?.web
        if (web != null && web.canGoBack()) web.goBack() else moveTaskToBack(true)
    }

    private fun configureWebView(tab: Tab) {
        val web = tab.web
        val slot = tab.slot
        val serverHost = { ServerUrl.host(Prefs.server(this, slot)) }
        val home = { ServerUrl.page(Prefs.server(this, slot), "/") }
        val s = web.settings
        s.javaScriptEnabled = true
        s.domStorageEnabled = true
        s.mediaPlaybackRequiresUserGesture = false
        s.allowFileAccess = false
        s.allowContentAccess = false
        s.setSupportZoom(false)
        s.cacheMode = WebSettings.LOAD_DEFAULT
        s.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
        s.userAgentString = s.userAgentString + " NoriApp/" + BuildConfig.VERSION_NAME
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, false)
        web.setBackgroundColor(Color.parseColor("#111111"))
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val uri = request.url
                when (uri.scheme) {
                    "nori-pair", "nori-app" -> {
                        when (val link = Deeplink.parse(uri.toString())) {
                            is Link.Pair -> confirmPair(link)
                            Link.Settings -> startActivity(Intent(this@MainActivity, SettingsActivity::class.java))
                            Link.AddTask -> startActivity(Intent(this@MainActivity, QuickTaskActivity::class.java))
                            Link.Chat -> view.loadUrl(home())
                            Link.Talk -> view.loadUrl(ServerUrl.page(Prefs.server(this@MainActivity, slot), Deeplink.TALK_PATH))
                            is Link.Prefill -> view.loadUrl(ServerUrl.page(Prefs.server(this@MainActivity, slot), Deeplink.prefillPath(link.text)))
                            Link.Unknown -> if (uri.host == "retry") view.loadUrl(home())
                        }
                        return true
                    }
                    "https" -> if (uri.host?.lowercase() == serverHost()) return false
                }
                // anything else leaves Nori: only ordinary web, mail and phone links are handed to Android
                if (uri.scheme == "https" || uri.scheme == "http" || uri.scheme == "mailto" || uri.scheme == "tel") {
                    try { startActivity(Intent(Intent.ACTION_VIEW, uri)) } catch (e: Exception) { /* nothing can open it */ }
                }
                return true
            }

            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (!request.isForMainFrame) return
                val html = "<html><body style='background:#111;color:#eee;font-family:sans-serif;padding:2rem'><h2>Can't reach ${Prefs.agent(this@MainActivity, slot)}</h2><p>${Prefs.server(this@MainActivity, slot)}</p>" +
                    "<p>${error.description}</p><p><a style='color:#9cf' href='nori-app://retry'>Try again</a> &nbsp; <a style='color:#9cf' href='nori-app://settings'>App settings</a></p></body></html>"
                view.loadDataWithBaseURL(null, html, "text/html", "utf-8", null)
            }
        }
        web.webChromeClient = object : WebChromeClient() {
            override fun onShowFileChooser(view: WebView, callback: ValueCallback<Array<Uri>>, params: FileChooserParams): Boolean {
                fileCallback?.onReceiveValue(null)
                fileCallback = callback
                return try { startActivityForResult(params.createIntent(), REQ_FILE); true } catch (e: Exception) { fileCallback = null; false }
            }

            /** The chat's push-to-talk uses the microphone. Only this connection's own pages may ask, and only for audio. */
            override fun onPermissionRequest(request: PermissionRequest) {
                val fromServer = request.origin?.host?.lowercase() == serverHost()
                val wantsOnlyAudio = request.resources.isNotEmpty() && request.resources.all { it == PermissionRequest.RESOURCE_AUDIO_CAPTURE }
                if (!fromServer || !wantsOnlyAudio) { request.deny(); return }
                if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) { request.grant(request.resources); return }
                pendingPermission?.deny()
                pendingPermission = request
                requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQ_MIC)
            }

            override fun onPermissionRequestCanceled(request: PermissionRequest) { if (pendingPermission === request) pendingPermission = null }
        }
        web.setDownloadListener { url, userAgent, contentDisposition, mime, _ ->
            try {
                val u = Uri.parse(url)
                if (u.scheme != "https" || u.host?.lowercase() != serverHost()) { Toast.makeText(this, "Downloads are only allowed from your server", Toast.LENGTH_SHORT).show(); return@setDownloadListener }
                val req = DownloadManager.Request(u)
                req.setMimeType(mime)
                req.addRequestHeader("Cookie", CookieManager.getInstance().getCookie(url) ?: "")
                req.addRequestHeader("User-Agent", userAgent)
                req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, URLUtil.guessFileName(url, contentDisposition, mime))
                (getSystemService(DOWNLOAD_SERVICE) as DownloadManager).enqueue(req)
                Toast.makeText(this, "Downloading…", Toast.LENGTH_SHORT).show()
            } catch (e: Exception) { Toast.makeText(this, "Couldn't download that", Toast.LENGTH_SHORT).show() }
        }
    }

    @Deprecated("the classic result API is the simplest way to hand the file picker back to the web page")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQ_FILE) {
            val cb = fileCallback
            fileCallback = null
            cb?.onReceiveValue(if (resultCode == RESULT_OK) WebChromeClient.FileChooserParams.parseResult(resultCode, data) else null)
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_MIC) {
            val req = pendingPermission
            pendingPermission = null
            if (req != null) { if (grantResults.isNotEmpty() && grantResults.all { it == PackageManager.PERMISSION_GRANTED }) req.grant(req.resources) else req.deny() }
        }
        if (requestCode == REQ_NOTIF) Notifier.refreshSync(this)
    }

    override fun onDestroy() {
        for (t in tabs.values) t.web.destroy()
        tabs.clear()
        super.onDestroy()
    }
}
