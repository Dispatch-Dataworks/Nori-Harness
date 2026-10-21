package app.nori.companion

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import androidx.core.content.FileProvider
import app.nori.companion.core.AppInfo
import app.nori.companion.core.UpdateLogic
import java.io.File

/**
 * Knowing the server has a newer build, fetching it with the device token from GET /device/app.apk, and handing it to Android's package installer.
 * Nothing installs without Android's own confirmation, and the token is only ever sent to the paired server (see UpdateLogic.resolveApkUrl).
 */
object AppUpdater {
    /** Called with every poll answer: remembers the version and the address, and tells the owner once per version. */
    fun noteFromPoll(c: Context, app: AppInfo) {
        if (app.latest.isBlank()) return
        Prefs.setLatestApp(c, app.latest)
        Prefs.setApkUrl(c, app.apkUrl)
        val quiet = Prefs.serverSettings(c).quiet
        if (!quiet && UpdateLogic.shouldNotify(app.latest, BuildConfig.VERSION_NAME, Prefs.updateNotified(c))) {
            Prefs.setUpdateNotified(c, app.latest)
            Notifier.showUpdate(c, app.latest)
            SentLog.add(c, "a newer app is on the server: ${app.latest} (this is ${BuildConfig.VERSION_NAME})")
        }
    }

    fun available(c: Context): String? = Prefs.latestApp(c).takeIf { UpdateLogic.isNewer(it, BuildConfig.VERSION_NAME) }

    /** Blocking (call off the main thread). Returns an error text, or null when the installer was opened. */
    fun downloadAndInstall(c: Context): String? {
        val app = c.applicationContext
        if (!Prefs.isPaired(app)) return "Pair this phone first."
        val url = UpdateLogic.resolveApkUrl(Prefs.server(app), Prefs.apkUrl(app).ifBlank { "/device/app.apk" }) ?: return "The server gave an update address outside itself, so it was not used."
        val file = File(File(app.cacheDir, "updates"), "nori-update.apk")
        SentLog.add(app, "downloading the update")
        val err = Api.download(app, url, file)
        if (err != null) { SentLog.add(app, "update download failed: $err"); return "Download failed: $err" }
        if (!app.packageManager.canRequestPackageInstalls()) {
            app.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + app.packageName)).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            return "Allow Nori to install updates, then press Update again (the file is already downloaded)."
        }
        val uri = FileProvider.getUriForFile(app, app.packageName + ".files", file)
        app.startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/vnd.android.package-archive").addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK))
        SentLog.add(app, "update downloaded; Android's installer opened")
        return null
    }
}
