package app.nori.companion.core

/** The one-line "what is this app doing right now" text at the top of the settings screen. */
object StatusLine {
    fun build(
        paired: Boolean,
        needsRepair: Boolean,
        paused: Boolean,
        now: Long,
        interactUntil: Long,
        shareUntil: Long,
        lastStatus: String,
        serverHost: String = "",
    ): String {
        if (!paired) return "Not paired yet. Enter the server address and a pairing code from Nori's Settings -> Devices."
        if (needsRepair) return "This phone was removed on the server (or the pairing expired). Pair it again."
        val parts = ArrayList<String>()
        parts.add(if (paused) "PAUSED: nothing is sent except a heartbeat, and Nori cannot do anything on this phone." else "Connected" + (if (serverHost.isNotEmpty()) " to $serverHost" else ""))
        if (!paused) {
            if (Windows.isOpen(interactUntil, now)) parts.add("Nori may act on this phone for ${Windows.countdown(Windows.remainingMs(interactUntil, now))} more.")
            if (Windows.isOpen(shareUntil, now)) parts.add("Screen share window open for ${Windows.countdown(Windows.remainingMs(shareUntil, now))} more.")
            if (lastStatus.isNotBlank() && !lastStatus.equals("Connected", true)) parts.add(lastStatus)
        }
        return parts.joinToString(" ")
    }
}
