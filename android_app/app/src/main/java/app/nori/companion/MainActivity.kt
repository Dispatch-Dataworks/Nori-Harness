package app.nori.companion

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.app.DownloadManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.PowerManager
import android.provider.Settings
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
import android.widget.Toast
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import app.nori.companion.core.Deeplink
import app.nori.companion.core.Link
import app.nori.companion.core.ServerUrl

/**
 * Nori in a full-screen web view: the chat, the settings, every page, signed in the way the website is (the website's own login; this app never sees the password).
 * Only the paired server's pages load here; links to other sites open in the phone's browser. Notifications, widgets and shortcuts open it at the chat.
 */
class MainActivity : Activity() {
    private lateinit var web: WebView
    private var fileCallback: ValueCallback<Array<Uri>>? = null
    private var pendingPermission: PermissionRequest? = null
    private val REQ_FILE = 71
    private val REQ_MIC = 72
    private val REQ_NOTIF = 74
    private var loadedServer = ""
    private var pendingPath: String? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WindowCompat.setDecorFitsSystemWindows(window, false)
        window.setBackgroundDrawable(android.graphics.drawable.ColorDrawable(Color.parseColor("#111111")))
        val root = FrameLayout(this)
        web = WebView(this)
        root.addView(web, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        setContentView(root)
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, wi ->
            val i = wi.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.ime())
            v.setPadding(i.left, i.top, i.right, i.bottom)
            WindowInsetsCompat.CONSUMED
        }
        configureWebView()
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
            Link.Chat -> pendingPath = Deeplink.CHAT_PATH
            Link.Talk -> pendingPath = Deeplink.TALK_PATH
            is Link.Prefill -> pendingPath = Deeplink.prefillPath(link.text)
            Link.Unknown -> {}
        }
        i?.data = null           // handled: do not run it again if the activity is recreated
    }

    /** A pairing link can come from anywhere, so the owner confirms which server before anything is sent to it. */
    private fun confirmPair(link: Link.Pair) {
        val server = link.server ?: Prefs.server(this).ifEmpty { null }
        if (server == null) { Toast.makeText(this, "That pairing link has no server address.", Toast.LENGTH_LONG).show(); startActivity(Intent(this, SettingsActivity::class.java)); return }
        AlertDialog.Builder(this)
            .setTitle("Pair this phone with ${ServerUrl.host(server)}?")
            .setMessage("Only continue if you made this pairing code yourself in Nori's Settings -> Devices. Pairing lets that server send you notifications; anything more is a separate switch you turn on here.")
            .setNegativeButton("Cancel", null)
            .setPositiveButton("Pair") { _, _ ->
                PairHelper.pair(this, server, link.code) { ok, msg ->
                    Toast.makeText(this, msg, Toast.LENGTH_LONG).show()
                    if (ok) { loadedServer = ""; onResume() } else startActivity(Intent(this, SettingsActivity::class.java))
                }
            }.show()
    }

    override fun onResume() {
        super.onResume()
        AppState.appVisible = true
        SyncService.pollNow(this)                                // tell the server the chat is in front, so it holds notifications back
        val server = Prefs.server(this)
        if (server.isEmpty() || !Prefs.isPaired(this)) {
            if (!Prefs.flag(this, "welcomed")) { Prefs.setFlag(this, "welcomed"); startActivity(Intent(this, SettingsActivity::class.java)) }
            else if (server.isEmpty()) startActivity(Intent(this, SettingsActivity::class.java))
        }
        if (server.isNotEmpty()) {
            if (pendingPath != null) loadPending()
            else if (loadedServer != server) { loadedServer = server; web.loadUrl(ServerUrl.page(server, "/")) }
        }
        if (Prefs.isPaired(this)) {
            SyncService.start(this)
            SyncService.requestHealth(this)
            askBatteryOnce()
        }
    }

    private fun loadPending() {
        val server = Prefs.server(this)
        val p = pendingPath ?: return
        pendingPath = null
        if (server.isEmpty()) return
        loadedServer = server
        web.loadUrl(ServerUrl.page(server, p))
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

    private fun goBackOrLeave() { if (web.canGoBack()) web.goBack() else moveTaskToBack(true) }

    private fun serverHost() = ServerUrl.host(Prefs.server(this))

    private fun configureWebView() {
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
                            Link.Chat -> view.loadUrl(ServerUrl.page(Prefs.server(this@MainActivity), "/"))
                            Link.Talk -> view.loadUrl(ServerUrl.page(Prefs.server(this@MainActivity), Deeplink.TALK_PATH))
                            is Link.Prefill -> view.loadUrl(ServerUrl.page(Prefs.server(this@MainActivity), Deeplink.prefillPath(link.text)))
                            Link.Unknown -> if (uri.host == "retry") view.loadUrl(ServerUrl.page(Prefs.server(this@MainActivity), "/"))
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
                val html = "<html><body style='background:#111;color:#eee;font-family:sans-serif;padding:2rem'><h2>Can't reach Nori</h2><p>${Prefs.server(this@MainActivity)}</p>" +
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

            /** The chat's push-to-talk uses the microphone. Only the paired server's own pages may ask, and only for audio. */
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
                if (u.scheme != "https" || u.host?.lowercase() != serverHost()) { Toast.makeText(this, "Downloads are only allowed from your Nori server", Toast.LENGTH_SHORT).show(); return@setDownloadListener }
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
        web.destroy()
        super.onDestroy()
    }
}
