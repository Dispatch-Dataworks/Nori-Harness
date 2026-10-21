package app.nori.companion.core

/** What the phone knows about one node on the screen, with no Android types so the rules are unit-testable. */
class FieldInfo(
    val isPassword: Boolean = false,
    val inputType: Int = 0,
    val editable: Boolean = false,
    val className: String = "",
    val viewId: String = "",
    /** What the field is called: its hint, content description, the label it is tied to. NOT what is typed in it. */
    val hint: String = "",
    /** The visible text (for an editable field: what has been typed). */
    val text: String = "",
    val autofillHints: List<String> = emptyList(),
)

/**
 * Password / card / one-time-code detection. A sensitive node is reported as "(sensitive field)" with no text, and is never typed into.
 * It errs on the side of hiding: a false positive costs a missing label, a false negative leaks a secret.
 */
object Sensitive {
    const val MASK = "(sensitive field)"

    // android.text.InputType constants, inlined so this file needs no Android classes.
    private const val CLASS_MASK = 0xf
    private const val VARIATION_MASK = 0xff0
    private const val CLASS_TEXT = 0x1
    private const val CLASS_NUMBER = 0x2
    private const val TEXT_PASSWORD = 0x80
    private const val TEXT_VISIBLE_PASSWORD = 0x90
    private const val TEXT_WEB_PASSWORD = 0xe0
    private const val NUMBER_PASSWORD = 0x10

    private val FIELD_WORDS = Regex(
        "password|passcode|passwd|passphrase|\\bpin\\b|\\bcvv2?\\b|\\bcvc2?\\b|\\bcvn\\b|security code|card number|card no\\b|credit card|debit card|expiry|expiration|\\bexp\\b|" +
            "\\botp\\b|one.?time|verification code|verify code|confirmation code|auth(entication)? code|\\b2fa\\b|two.?factor|sms code|login code|sign.?in code|\\bssn\\b|social security|" +
            "routing number|account number|\\biban\\b|\\bsecret\\b|recovery phrase|seed phrase|private key|api key|\\btoken\\b|cardnumber|card_number|securitycode",
        RegexOption.IGNORE_CASE,
    )
    private val CODE_TALK = Regex("(code|otp|passcode|verification|verify|\\bpin\\b|one-time|2fa|security|password)", RegexOption.IGNORE_CASE)
    private val DIGIT_RUN = Regex("(?<![\\d:.,/])\\d{4,8}(?![\\d:.,/])")
    private val CARD_LIKE = Regex("^[\\d][\\d \\-]{11,22}[\\d]$")

    fun inputTypeIsSecret(inputType: Int): Boolean {
        val cls = inputType and CLASS_MASK
        val variation = inputType and VARIATION_MASK
        if (cls == CLASS_TEXT && (variation == TEXT_PASSWORD || variation == TEXT_VISIBLE_PASSWORD || variation == TEXT_WEB_PASSWORD)) return true
        if (cls == CLASS_NUMBER && variation == NUMBER_PASSWORD) return true
        return false
    }

    fun luhn(digits: String): Boolean {
        if (digits.length < 13 || digits.length > 19 || !digits.all { it.isDigit() }) return false
        var sum = 0
        var alt = false
        for (i in digits.indices.reversed()) {
            var d = digits[i] - '0'
            if (alt) { d *= 2; if (d > 9) d -= 9 }
            sum += d
            alt = !alt
        }
        return sum % 10 == 0
    }

    fun looksLikeCardNumber(s: String): Boolean {
        val t = s.trim()
        if (!CARD_LIKE.matches(t)) return false
        return luhn(t.filter { it.isDigit() })
    }

    /** A message that talks about a code and contains a 4-8 digit run ("Your verification code is 482913"). */
    fun looksLikeCodeMessage(s: String): Boolean = s.isNotEmpty() && CODE_TALK.containsMatchIn(s) && DIGIT_RUN.containsMatchIn(s)

    fun isSensitive(f: FieldInfo): Boolean {
        if (f.isPassword) return true
        if (inputTypeIsSecret(f.inputType)) return true
        for (h in f.autofillHints) {
            val x = h.lowercase()
            if (x.contains("password") || x.startsWith("creditcard") || x.contains("otp") || x.contains("securitycode") || x == "pin" || x.contains("newpassword")) return true
        }
        if (f.editable) {
            if (FIELD_WORDS.containsMatchIn(f.hint)) return true
            if (FIELD_WORDS.containsMatchIn(splitIdentifier(f.viewId))) return true
            if (f.className.contains("password", ignoreCase = true)) return true
        }
        // Whatever the node is, a card number or a one-time-code message is not read out.
        if (looksLikeCardNumber(f.text) || looksLikeCardNumber(f.hint)) return true
        if (looksLikeCodeMessage(f.text)) return true
        return false
    }

    /** "btn_cardNumber" -> "btn card Number" so word-boundary rules see the words. */
    fun splitIdentifier(id: String): String =
        id.substringAfter(":id/").replace(Regex("([a-z0-9])([A-Z])"), "$1 $2").replace('_', ' ').replace('-', ' ').replace('.', ' ')

    /** Whether text to be typed would itself be a secret the phone should not type (a card number, a code message). */
    fun textIsSecret(text: String): Boolean = looksLikeCardNumber(text)
}
