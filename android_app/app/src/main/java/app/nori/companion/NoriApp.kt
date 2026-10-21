package app.nori.companion

import android.app.Application
import android.content.Context
import java.io.File
import java.io.PrintWriter
import java.io.StringWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** The app's process: notes an uncaught crash in the sent log and keeps the trace for the "Copy the log" button, then lets Android carry on. */
class NoriApp : Application() {
    override fun onCreate() {
        super.onCreate()
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { t, e ->
            try {
                val sw = StringWriter()
                e.printStackTrace(PrintWriter(sw))
                File(filesDir, CRASH_FILE).writeText(SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(Date()) + "  thread " + t.name + "  app " + BuildConfig.VERSION_NAME + "\n" + sw.toString().take(6000))
                SentLog.add(this, "CRASH (${e.javaClass.simpleName}): ${(e.message ?: "").take(120)}  at ${e.stackTrace.firstOrNull() ?: "?"}")
            } catch (x: Throwable) { /* never make a crash worse */ }
            previous?.uncaughtException(t, e)
        }
    }

    companion object {
        const val CRASH_FILE = "last_crash.txt"
        fun lastCrash(c: Context): String? = try { File(c.filesDir, CRASH_FILE).takeIf { it.exists() }?.readText() } catch (e: Exception) { null }
        fun clearCrash(c: Context) { try { File(c.filesDir, CRASH_FILE).delete() } catch (e: Exception) { /* nothing */ } }
    }
}
