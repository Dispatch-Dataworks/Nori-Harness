package app.nori.companion

import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.core.app.Person
import androidx.core.content.pm.ShortcutInfoCompat
import androidx.core.content.pm.ShortcutManagerCompat
import androidx.core.graphics.drawable.IconCompat
import app.nori.companion.core.Channels
import app.nori.companion.core.Deeplink

/**
 * One shortcut per paired connection, named after its assistant (2026-11-10). It does two jobs: it puts "Nori" (or whatever the assistant is called) in the top row of Android's Share
 * sheet, going straight to that connection (shortcuts.xml's share-target, ShareActivity), and it is the conversation the message notifications belong to, so Android treats them as a
 * conversation. A connection that is no longer usable loses its shortcut.
 */
object Shortcuts {
    private var published = emptyMap<Int, String>()

    fun person(c: Context, slot: Int): Person = Person.Builder().setName(Prefs.agent(c, slot)).setKey(Channels.shortcutId(slot)).setImportant(true).build()

    @Synchronized fun publish(c: Context) {
        val want = Prefs.usableSlots(c).associateWith { Prefs.agent(c, it) }
        if (want == published) return
        try {
            for ((slot, name) in want) {
                if (published[slot] == name) continue
                val open = Intent(Intent.ACTION_VIEW, Uri.parse(Deeplink.chatLink(slot))).setClass(c, MainActivity::class.java)
                val s = ShortcutInfoCompat.Builder(c, Channels.shortcutId(slot))
                    .setShortLabel(name.take(25)).setLongLabel("Send to $name".take(50))
                    .setIcon(IconCompat.createWithResource(c, R.mipmap.ic_launcher))
                    .setIntent(open).setLongLived(true).setPerson(person(c, slot))
                    .setCategories(setOf(Channels.SHARE_CATEGORY))
                    .build()
                ShortcutManagerCompat.pushDynamicShortcut(c, s)
            }
            val gone = published.keys - want.keys
            if (gone.isNotEmpty()) ShortcutManagerCompat.removeLongLivedShortcuts(c, gone.map { Channels.shortcutId(it) })
            published = want
        } catch (e: Exception) { SentLog.add(c, "the share shortcut could not be set: ${e.javaClass.simpleName}") }
    }

    fun has(slot: Int): Boolean = slot in published
}
