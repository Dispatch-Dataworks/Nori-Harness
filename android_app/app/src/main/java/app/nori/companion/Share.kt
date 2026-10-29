package app.nori.companion

import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Base64
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.IntentCompat
import androidx.exifinterface.media.ExifInterface
import app.nori.companion.core.Connections
import app.nori.companion.core.ShareBody
import org.json.JSONObject
import java.io.ByteArrayOutputStream

/** Turns a picture shared from another app into the upright, downscaled JPEG the server takes. */
object PhotoShare {
    private const val MAX_SIDE = 1920
    private const val MAX_BYTES = 6_000_000

    /** Base64 JPEG, or null when the picture cannot be read. Honours the EXIF rotation so a portrait shot is not sent sideways. */
    fun prepare(c: Context, uri: Uri): String? {
        val raw = c.contentResolver.openInputStream(uri)?.use { it.readBytes() } ?: return null
        val opts = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(raw, 0, raw.size, opts)
        if (opts.outWidth <= 0 || opts.outHeight <= 0) return null
        var sample = 1
        while (maxOf(opts.outWidth, opts.outHeight) / sample > MAX_SIDE) sample *= 2
        var bmp = BitmapFactory.decodeByteArray(raw, 0, raw.size, BitmapFactory.Options().apply { inSampleSize = sample }) ?: return null
        val rotation = try {
            when (ExifInterface(raw.inputStream()).getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)) {
                ExifInterface.ORIENTATION_ROTATE_90 -> 90f
                ExifInterface.ORIENTATION_ROTATE_180 -> 180f
                ExifInterface.ORIENTATION_ROTATE_270 -> 270f
                else -> 0f
            }
        } catch (e: Exception) { 0f }
        if (rotation != 0f) bmp = Bitmap.createBitmap(bmp, 0, 0, bmp.width, bmp.height, Matrix().apply { postRotate(rotation) }, true)
        var quality = 85
        var bytes: ByteArray
        do {
            val out = ByteArrayOutputStream()
            bmp.compress(Bitmap.CompressFormat.JPEG, quality, out)
            bytes = out.toByteArray()
            quality -= 15
        } while (bytes.size > MAX_BYTES && quality >= 25)
        bmp.recycle()
        return Base64.encodeToString(bytes, Base64.NO_WRAP)
    }
}

/** Which of the phone's connections a share goes to. With one it is that one; with several the sheet asks, starting from the one used last (or the tab in front). */
object ShareTarget {
    /** The connections a share can go to now. */
    fun options(c: Context): List<Int> = Prefs.usableSlots(c)

    fun default(c: Context, options: List<Int>): Int = Connections.pick(Prefs.lastShare(c).takeIf { it >= 0 }, options, Prefs.selected(c))

    /** A drop-down of the connections, or null when there is only one and nothing to ask. */
    fun picker(a: Activity, options: List<Int>): Spinner? {
        if (options.size < 2) return null
        val names = options.map { Prefs.name(a, it) }
        return Spinner(a).apply {
            adapter = ArrayAdapter(a, android.R.layout.simple_spinner_dropdown_item, names)
            setSelection(options.indexOf(default(a, options)).coerceAtLeast(0))
        }
    }

    fun chosen(spinner: Spinner?, options: List<Int>, fallback: Int): Int = if (spinner == null) fallback else options.getOrElse(spinner.selectedItemPosition) { fallback }
}

/** Sends a share body and says what happened in the sent log and a toast. Call off the main thread. */
object ShareSender {
    fun send(c: Context, slot: Int, body: JSONObject): String? {
        val r = Api.post(c, slot, "/device/share", body, 60_000)
        val kind = body.optString("kind")
        val dest = body.optString("dest")
        val who = if (Prefs.slots(c).size > 1) " (${Prefs.name(c, slot)})" else ""
        SentLog.add(c, if (r.ok) "shared a $kind to your $dest$who" else "share failed$who: ${r.error}")
        if (r.ok) Prefs.setLastShare(c, slot)
        return if (r.ok) null else (r.error ?: "could not send")
    }

    fun toast(c: Context, text: String, long: Boolean = false) { Handler(Looper.getMainLooper()).post { Toast.makeText(c.applicationContext, text, if (long) Toast.LENGTH_LONG else Toast.LENGTH_SHORT).show() } }
}

/**
 * "Send to Nori" in any app's Share sheet. A small chooser asks where it goes (the chat by default, a note, or a task), then it sends in the background and a toast says whether it worked.
 * Nothing is sent unless you chose to share it.
 */
class ShareActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val app = applicationContext
        val i = intent
        if (i?.action != Intent.ACTION_SEND) { finish(); return }
        val targets = ShareTarget.options(app)
        if (targets.isEmpty()) { Toast.makeText(this, "Pair this phone with Nori first", Toast.LENGTH_LONG).show(); startActivity(Intent(this, SettingsActivity::class.java)); finish(); return }
        if (Prefs.paused(app)) { Toast.makeText(this, "Nori is paused on this phone", Toast.LENGTH_LONG).show(); finish(); return }
        if (!Prefs.ownerShare(app)) { Toast.makeText(this, "Sharing to Nori is switched off in the Nori app", Toast.LENGTH_LONG).show(); finish(); return }
        val type = i.type ?: ""
        val text = i.getStringExtra(Intent.EXTRA_TEXT) ?: ""
        val subject = i.getStringExtra(Intent.EXTRA_SUBJECT) ?: i.getStringExtra(Intent.EXTRA_TITLE) ?: ""
        val stream = IntentCompat.getParcelableExtra(i, Intent.EXTRA_STREAM, Uri::class.java)
        val isImage = type.startsWith("image/") && stream != null
        if (!isImage && text.isBlank()) { Toast.makeText(this, "Nothing to send to Nori", Toast.LENGTH_SHORT).show(); finish(); return }

        val dests = listOf("chat" to "My chat with Nori", "note" to "A note", "task" to "A task")
        val group = RadioGroup(this).apply { orientation = RadioGroup.VERTICAL }
        dests.forEachIndexed { idx, d -> group.addView(RadioButton(this).apply { id = 1000 + idx; this.text = d.second; isChecked = idx == 0 }) }
        val note = EditText(this).apply { hint = "Add a note (optional)"; isSingleLine = false; maxLines = 3 }
        val preview = TextView(this).apply { this.text = if (isImage) "A photo" else (subject.ifBlank { text }).trim().take(140); setPadding(0, 0, 0, 12) }
        val which = ShareTarget.picker(this, targets)
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL; setPadding(56, 24, 56, 0)
            if (which != null) addView(which)
            addView(preview); addView(group); addView(note, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        }

        AlertDialog.Builder(this).setTitle("Send to Nori").setView(col)
            .setNegativeButton("Cancel") { _, _ -> finish() }
            .setPositiveButton("Send") { _, _ ->
                val dest = dests[(group.checkedRadioButtonId - 1000).coerceIn(0, dests.size - 1)].first
                val noteText = note.text.toString()
                val slot = ShareTarget.chosen(which, targets, targets.first())
                val sentTo = if (which != null) " to ${Prefs.name(app, slot)}" else " to Nori"
                Thread {
                    val body = try {
                        if (isImage) PhotoShare.prepare(app, stream!!)?.let { ShareBody.image(it, text, dest, noteText) } else ShareBody.fromText(text, subject, dest, noteText)
                    } catch (e: Exception) { null }
                    if (body == null) { ShareSender.toast(app, "Couldn't read that to send it"); return@Thread }
                    val err = ShareSender.send(app, slot, body)
                    ShareSender.toast(app, if (err == null) "Sent$sentTo" else "Couldn't send: $err", long = err != null)
                }.start()
                finish()
            }
            .setOnCancelListener { finish() }
            .show()
    }
}

/** The "Add task" widget and shortcut: a one-line prompt that sends what you type as a task. */
class QuickTaskActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val app = applicationContext
        val targets = ShareTarget.options(app)
        if (targets.isEmpty()) { Toast.makeText(this, "Pair this phone with Nori first", Toast.LENGTH_LONG).show(); startActivity(Intent(this, SettingsActivity::class.java)); finish(); return }
        if (Prefs.paused(app)) { Toast.makeText(this, "Nori is paused on this phone", Toast.LENGTH_LONG).show(); finish(); return }
        val input = EditText(this).apply { hint = "What needs doing?"; isSingleLine = true; setPadding(56, 32, 56, 16) }
        val which = ShareTarget.picker(this, targets)
        val form = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(56, 24, 56, 0); if (which != null) addView(which); addView(input) }
        AlertDialog.Builder(this).setTitle("Add a task").setView(form)
            .setNegativeButton("Cancel") { _, _ -> finish() }
            .setPositiveButton("Add") { _, _ ->
                val body = ShareBody.fromText(input.text.toString(), null, "task")
                val slot = ShareTarget.chosen(which, targets, targets.first())
                if (body == null) ShareSender.toast(app, "Nothing to add")
                else Thread {
                    val err = ShareSender.send(app, slot, body)
                    ShareSender.toast(app, if (err == null) "Task added" + (if (which != null) " for ${Prefs.name(app, slot)}" else "") else "Couldn't add it: $err", long = err != null)
                }.start()
                finish()
            }
            .setOnCancelListener { finish() }
            .show()
        input.requestFocus()
        window.setSoftInputMode(android.view.WindowManager.LayoutParams.SOFT_INPUT_STATE_VISIBLE)
    }
}
