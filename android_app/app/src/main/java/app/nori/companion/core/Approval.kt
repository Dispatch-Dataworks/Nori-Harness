package app.nori.companion.core

/** The phone's own judgement of whether a control is risky enough to pause for the owner. Nothing is done when it says yes. */
class Risk(val needsApproval: Boolean, val reason: String = "") {
    companion object {
        val SAFE = Risk(false)
    }
}

object Approval {
    // verb stem -> why that is risky. Matched against whole words and inflections of the label / description / view id.
    private val VERBS: List<Pair<String, String>> = listOf(
        "send" to "this sends something", "submit" to "this submits something", "post" to "this posts something publicly", "publish" to "this publishes something",
        "tweet" to "this posts something publicly",
        "pay" to "this spends money", "buy" to "this buys something", "purchase" to "this buys something", "order" to "this places an order",
        "checkout" to "this goes to payment", "check out" to "this goes to payment", "subscribe" to "this signs up for something", "donate" to "this gives money",
        "transfer" to "this moves money", "withdraw" to "this moves money", "deposit" to "this moves money", "book" to "this makes a booking",
        "reserve" to "this makes a booking", "delete" to "this deletes something", "remove" to "this removes something", "erase" to "this erases something", "wipe" to "this erases something",
        "unsubscribe" to "this changes a subscription", "uninstall" to "this uninstalls an app", "install" to "this installs software",
        "confirm" to "this confirms something", "accept" to "this accepts something", "agree" to "this agrees to something", "allow" to "this grants permission", "grant" to "this grants permission",
        "authorize" to "this grants permission", "authorise" to "this grants permission", "approve" to "this approves something", "sign up" to "this creates an account or signs up",
        "register" to "this creates an account or signs up", "reset" to "this resets something",
    )

    // words that merely start like a risky verb
    private val NOT_RISKY_WORDS = setOf(
        "postal", "postcode", "postpone", "postponed", "posture", "poster", "border", "orders", "ordered", "bookmark", "bookmarks", "booking", "bookings",
        "registry", "reserved", "presenter", "payload",
    )

    private val WORD_RE = Regex("[a-z]+")

    /** Lowercase words of a label or view id ("sendButton" -> send button, "btn_pay_now" -> btn pay now). */
    fun words(text: String): List<String> {
        // fold full-width letters and accents ("Ｓｅｎｄ", "Sénd") so look-alike spellings are still read as the word
        val folded = java.text.Normalizer.normalize(text, java.text.Normalizer.Form.NFKD).replace(Regex("\\p{M}+"), "")
        val spaced = Sensitive.splitIdentifier(folded).replace(Regex("([a-z])([A-Z])"), "$1 $2")
        return WORD_RE.findAll(spaced.lowercase()).map { it.value }.toList()
    }

    /** The reason this label set is risky, or null. */
    fun riskyReason(labels: List<String>, viewId: String = ""): String? {
        val all = ArrayList<String>()
        for (l in labels) all.addAll(words(l))
        all.addAll(words(viewId))
        val joined = " " + all.joinToString(" ") + " "
        for ((verb, why) in VERBS) {
            if (verb.contains(' ')) { if (joined.contains(" $verb ") || joined.contains(" ${verb.replace(" ", "")} ")) return why; continue }
            for (w in all) {
                if (w in NOT_RISKY_WORDS) continue
                if (w == verb || (w.length > verb.length && w.startsWith(verb) && verb.length >= 3 && isInflection(w.substring(verb.length)))) return why
            }
        }
        return null
    }

    // "send" matches send, sends, sending, sender; "pay" matches paying, payment, payments
    private fun isInflection(rest: String): Boolean = rest in INFLECTIONS

    private val INFLECTIONS = setOf("s", "es", "ed", "d", "ing", "er", "ers", "ment", "ments", "ion", "ions", "ation", "ations", "ted", "ting")

    private val SEND_LIKE_FIELD = Regex("message|chat|comment|reply|compose|caption|\\bpost\\b|tweet|write|say something|type a|whatsapp|sms|text message|status|\\bnote\\b|bio|review", RegexOption.IGNORE_CASE)
    private val SEARCH_LIKE_FIELD = Regex("search|find|\\burl\\b|address|query|where to|filter", RegexOption.IGNORE_CASE)

    fun isSendLikeField(f: FieldInfo): Boolean = SEND_LIKE_FIELD.containsMatchIn(f.hint + " " + Sensitive.splitIdentifier(f.viewId))
    fun isSearchLikeField(f: FieldInfo): Boolean = SEARCH_LIKE_FIELD.containsMatchIn(f.hint + " " + Sensitive.splitIdentifier(f.viewId))

    /** A tap: risky if the control (or the thing it sits in) is called anything risky. */
    fun classifyTap(labels: List<String>, viewId: String): Risk {
        val why = riskyReason(labels, viewId) ?: return Risk.SAFE
        return Risk(true, why)
    }

    /** Typing: only the text itself is never risky, but ending it in Enter submits whatever the field belongs to. */
    fun classifyType(text: String, field: FieldInfo?): Risk {
        if (text.endsWith("\n") || text.endsWith("\r")) {
            return if (field != null && isSendLikeField(field)) Risk(true, "this types a message and presses Enter, which sends it") else Risk(true, "this types text and presses Enter, which submits it")
        }
        return Risk.SAFE
    }

    /** Pressing Enter / Go in the focused field: fine in a search or address box, otherwise it submits or sends. */
    fun classifyEnter(field: FieldInfo?): Risk {
        if (field != null && isSearchLikeField(field) && !isSendLikeField(field)) return Risk.SAFE
        if (field != null && isSendLikeField(field)) return Risk(true, "this sends a message")
        return Risk(true, "this presses Enter, which may submit or send something")
    }

    /** `button "Send"`, or `button (btn_send)` when nothing is written on it. Quotes in labels are neutralised. */
    fun describe(role: String, label: String, viewId: String): String {
        val l = label.replace('"', '\'').replace(Regex("\\s+"), " ").trim().take(80)
        if (l.isNotEmpty()) return "$role \"$l\""
        val id = viewId.substringAfter(":id/").take(40)
        return if (id.isNotEmpty()) "$role ($id)" else role
    }

    /** A short stable tag for "this exact control on this exact screen"; the approval round trip re-checks it so a changed screen is never acted on. */
    fun fingerprint(pkg: String, role: String, label: String, viewId: String, occurrence: Int): String {
        val norm = listOf(pkg.lowercase(), role, label.lowercase().replace(Regex("\\s+"), " ").trim(), viewId.substringAfter(":id/"), occurrence.toString()).joinToString("|")
        var h = 0x811c9dc5.toInt()
        for (ch in norm) { h = h xor ch.code; h *= 0x01000193 }
        return String.format(java.util.Locale.US, "%08x", h)
    }
}
