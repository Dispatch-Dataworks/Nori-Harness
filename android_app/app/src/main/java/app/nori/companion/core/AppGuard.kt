package app.nori.companion.core

/**
 * Which apps the phone refuses to read, describe, screenshot or control, whatever the server says.
 * Three layers: the built-in deny list (the phone's own security surfaces), the server's `hidden_apps`
 * (apps the owner wants treated as private) and `never_control_apps` (extra deny list). Plain Kotlin, no Android types.
 */
object AppGuard {
    const val SELF_PACKAGE = "app.nori.companion"
    const val PRIVATE_LABEL = "a private app"

    /** Packages that match exactly. */
    val DENY_EXACT: Set<String> = setOf(
        SELF_PACKAGE,
        // Settings, permission controllers, installers, stores: acting there could loosen the app's own limits or install software.
        "com.android.settings", "com.google.android.permissioncontroller", "com.android.permissioncontroller",
        "com.google.android.packageinstaller", "com.android.packageinstaller", "com.samsung.android.packageinstaller",
        "com.android.vending", "com.sec.android.app.samsungapps", "org.fdroid.fdroid", "com.aurora.store",
        "com.android.certinstaller", "com.android.keychain", "com.android.managedprovisioning",
        // System surfaces (notification shade, quick settings, lock screen) and Play services (which hosts Google Pay, passkey and password UI).
        "com.android.systemui", "com.google.android.gms", "com.google.android.gsf", "com.android.credentialmanager", "com.google.android.credentialmanager",
        "com.google.android.marvin.talkback", "com.android.shell",
        // Wallets and payment
        "com.google.android.apps.walletnfcrel", "com.google.android.apps.nbu.paisa.user", "com.samsung.android.spay", "com.samsung.android.samsungpass",
        // Password managers and authenticators
        "com.x8bit.bitwarden", "com.bitwarden.authenticator", "com.lastpass.lpandroid", "com.agilebits.onepassword", "com.onepassword.android",
        "com.dashlane", "com.callpod.android_apps.keeper", "org.keepassdroid", "com.kunzisoft.keepass.free", "com.kunzisoft.keepass.pro", "com.beemdevelopment.aegis",
        "com.google.android.apps.authenticator2", "com.azure.authenticator", "com.authy.authy", "com.duosecurity.duomobile", "org.fedorahosted.freeotp",
        "com.twofasapp", "io.ente.auth", "proton.android.pass", "com.yubico.yubioath", "com.okta.android.auth", "com.enpass.android", "io.enpass.app",
        "com.nordpass.android.app.password.manager", "com.kee.android",
    )

    /** Package families: matches the prefix itself or anything under it ("com.android.settings" also covers "com.android.settings.intelligence"). */
    val DENY_PREFIXES: List<String> = listOf(
        "com.android.settings", "com.samsung.android.settings", "com.samsung.android.app.settings", "com.google.android.settings", "com.miui.securitycenter",
        "com.lastpass", "com.bitwarden", "com.keepersecurity", "keepass2android", "com.agilebits", "com.nordpass", "com.proton.pass", "com.dashlane",
    )

    fun normalize(pkg: String): String = pkg.trim().lowercase()

    /** Server-supplied patterns: "*" (everything), "com.bank.*" (that family) or an exact package name. */
    fun matchesPattern(pkg: String, pattern: String): Boolean {
        val p = normalize(pkg)
        val h = normalize(pattern)
        if (p.isEmpty() || h.isEmpty()) return false
        if (h == "*") return true
        if (h.endsWith(".*")) {
            val base = h.dropLast(2)
            return p == base || p.startsWith("$base.")
        }
        return p == h
    }

    fun isBuiltInDenied(pkg: String): Boolean {
        val p = normalize(pkg)
        if (p.isEmpty()) return false
        if (p in DENY_EXACT) return true
        return DENY_PREFIXES.any { p == it || p.startsWith("$it.") }
    }

    fun isHidden(pkg: String, hidden: Collection<String>): Boolean = hidden.any { matchesPattern(pkg, it) }
    fun isNeverControl(pkg: String, never: Collection<String>): Boolean = never.any { matchesPattern(pkg, it) }

    class Verdict(val allowed: Boolean, val reason: String, val privateApp: Boolean = false)

    /** May the phone look at / act in the app in front? An unknown foreground app is refused (nothing can be said about it). */
    fun check(pkg: String?, hidden: Collection<String>, neverControl: Collection<String>): Verdict {
        val p = normalize(pkg ?: "")
        if (p.isEmpty()) return Verdict(false, "the app in front could not be identified, so nothing was done")
        if (isHidden(p, hidden)) return Verdict(false, "$PRIVATE_LABEL is in front, so nothing was done", privateApp = true)
        if (isBuiltInDenied(p)) return Verdict(false, "this phone never reads or acts in that app (settings, permissions, installers, the app store, wallet, password managers, system screens and Nori itself)")
        if (isNeverControl(p, neverControl)) return Verdict(false, "the owner keeps that app off limits")
        return Verdict(true, "")
    }

    /** What a launcher-visible app may be listed / opened as: not hidden, not denied, not the app itself. */
    fun listable(pkg: String, hidden: Collection<String>, neverControl: Collection<String>): Boolean =
        check(pkg, hidden, neverControl).allowed

    private val PKG_RE = Regex("^[a-zA-Z][a-zA-Z0-9_]*(\\.[a-zA-Z0-9_]+)+$")
    fun isValidPackageName(p: String): Boolean = p.length <= 255 && PKG_RE.matches(p)
}
