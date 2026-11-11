package app.nori.companion

import android.content.Context
import android.os.Handler
import android.os.Looper
import androidx.car.app.connection.CarConnection
import androidx.lifecycle.Observer

/**
 * Whether Android Auto is connected (2026-11-10): the one thing this app learns about a car. It goes to every server in the poll snapshot (`car`), so the assistant knows its owner is
 * driving and answers in short spoken lines. Only the projection kind counts (this phone running Android Auto); nothing else about the car is read. A change hangs up the current long
 * polls so the next one carries it at once.
 */
object CarWatcher {
    @Volatile var connected = false
        private set
    private var connection: CarConnection? = null
    private var appContext: Context? = null
    private val main = Handler(Looper.getMainLooper())
    private val observer = Observer<Int> { type ->
        val on = type == CarConnection.CONNECTION_TYPE_PROJECTION
        val c = appContext ?: return@Observer
        if (on != connected) {
            connected = on
            SentLog.add(c, if (on) "Android Auto connected: told ${Prefs.agent(c)} you are driving" else "Android Auto disconnected")
            SyncService.pollNow(c)
        }
    }

    fun start(c: Context) {
        main.post {
            if (connection != null) return@post
            try {
                appContext = c.applicationContext
                connection = CarConnection(c.applicationContext).also { it.type.observeForever(observer) }
            } catch (e: Exception) { SentLog.add(c, "the car connection could not be watched: ${e.javaClass.simpleName}") }
        }
    }

    fun stop() {
        main.post {
            try { connection?.type?.removeObserver(observer) } catch (e: Exception) { /* gone */ }
            connection = null
            connected = false
        }
    }
}
