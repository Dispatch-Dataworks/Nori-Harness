package app.nori.companion

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.widget.RemoteViews
import app.nori.companion.core.Deeplink

/**
 * The home-screen widget: four one-tap buttons. "Talk" opens the chat ready for push-to-talk; "Add task" opens a one-line prompt; "Water" and "Weight" open the chat with a
 * starter sentence already in the box (the web view goes to /?prefill=<text>). Every button just opens the app: nothing is sent until you press send.
 */
class NoriWidget : AppWidgetProvider() {
    override fun onUpdate(context: Context, manager: AppWidgetManager, ids: IntArray) {
        for (id in ids) manager.updateAppWidget(id, build(context))
    }

    private fun link(c: Context, req: Int, uri: String): PendingIntent =
        PendingIntent.getActivity(c, req, Intent(Intent.ACTION_VIEW, Uri.parse(uri)).setClass(c, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)

    private fun build(c: Context): RemoteViews {
        val v = RemoteViews(c.packageName, R.layout.widget)
        v.setOnClickPendingIntent(R.id.w_talk, link(c, 41, "nori-app://talk"))
        v.setOnClickPendingIntent(R.id.w_task, link(c, 42, "nori-app://task"))
        v.setOnClickPendingIntent(R.id.w_water, link(c, 43, Deeplink.prefillLink(PREFILL_WATER)))
        v.setOnClickPendingIntent(R.id.w_weight, link(c, 44, Deeplink.prefillLink(PREFILL_WEIGHT)))
        return v
    }

    companion object {
        const val PREFILL_WATER = "Log water: "
        const val PREFILL_WEIGHT = "Log weight: "
    }
}
