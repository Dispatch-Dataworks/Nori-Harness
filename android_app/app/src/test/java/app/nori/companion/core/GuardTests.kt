package app.nori.companion.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class AppGuardTest {
    @Test fun builtInDenyListCoversTheSecuritySurfaces() {
        val denied = listOf(
            "com.android.settings", "com.android.settings.intelligence", "com.google.android.permissioncontroller", "com.android.permissioncontroller",
            "com.google.android.packageinstaller", "com.android.packageinstaller", "com.android.vending", "com.google.android.apps.walletnfcrel",
            "com.google.android.apps.nbu.paisa.user", "com.x8bit.bitwarden", "com.lastpass.lpandroid", "com.agilebits.onepassword", "com.google.android.apps.authenticator2",
            "com.azure.authenticator", "com.authy.authy", "com.android.systemui", "app.nori.companion", "com.google.android.gms",
        )
        for (p in denied) assertTrue("$p should be denied", AppGuard.isBuiltInDenied(p))
    }

    @Test fun ordinaryAppsAreNotDenied() {
        for (p in listOf("com.google.android.apps.maps", "com.android.chrome", "com.spotify.music", "org.mozilla.firefox", "com.google.android.deskclock"))
            assertFalse("$p should be allowed", AppGuard.isBuiltInDenied(p))
    }

    @Test fun lookAlikePackagesAreNotConfusedWithTheDenyList() {
        assertFalse(AppGuard.isBuiltInDenied("com.android.settingsfake"))
        assertFalse(AppGuard.isBuiltInDenied("com.example.com.android.settings"))
        assertTrue(AppGuard.isBuiltInDenied("COM.ANDROID.SETTINGS"))          // case does not help
        assertTrue(AppGuard.isBuiltInDenied("  com.android.settings  "))
    }

    @Test fun hiddenAppsSupportExactWildcardAndFamilyPatterns() {
        val hidden = listOf("com.bank.app", "com.broker.*")
        assertTrue(AppGuard.isHidden("com.bank.app", hidden))
        assertTrue(AppGuard.isHidden("com.broker.trading", hidden))
        assertTrue(AppGuard.isHidden("com.broker", hidden))
        assertFalse(AppGuard.isHidden("com.bank.app2", hidden))
        assertFalse(AppGuard.isHidden("com.brokerage.x", hidden))
        assertTrue(AppGuard.isHidden("anything.at.all", listOf("*")))
        assertFalse(AppGuard.isHidden("com.bank.app", emptyList()))
    }

    @Test fun checkRefusesHiddenWithoutNamingTheApp() {
        val v = AppGuard.check("com.bank.app", listOf("com.bank.app"), emptyList())
        assertFalse(v.allowed)
        assertTrue(v.privateApp)
        assertTrue(v.reason.contains("private app"))
        assertFalse(v.reason.contains("bank"))
    }

    @Test fun checkRefusesDeniedNeverControlAndUnknown() {
        assertFalse(AppGuard.check("com.android.settings", emptyList(), emptyList()).allowed)
        assertFalse(AppGuard.check("com.example.game", emptyList(), listOf("com.example.game")).allowed)
        assertFalse(AppGuard.check(null, emptyList(), emptyList()).allowed)
        assertFalse(AppGuard.check("", emptyList(), emptyList()).allowed)
        assertTrue(AppGuard.check("com.google.android.apps.maps", listOf("com.bank.app"), listOf("com.example.game")).allowed)
    }

    @Test fun listableMeansNotHiddenNotDeniedNotSelf() {
        assertFalse(AppGuard.listable(AppGuard.SELF_PACKAGE, emptyList(), emptyList()))
        assertFalse(AppGuard.listable("com.bank.app", listOf("com.bank.app"), emptyList()))
        assertTrue(AppGuard.listable("com.spotify.music", listOf("com.bank.app"), emptyList()))
    }

    @Test fun packageNameValidation() {
        assertTrue(AppGuard.isValidPackageName("com.google.android.apps.maps"))
        assertFalse(AppGuard.isValidPackageName("maps"))
        assertFalse(AppGuard.isValidPackageName("com.x; rm -rf"))
        assertFalse(AppGuard.isValidPackageName("com..x"))
        assertFalse(AppGuard.isValidPackageName(""))
    }
}

class SensitiveTest {
    @Test fun passwordFlagAlwaysMasks() {
        assertTrue(Sensitive.isSensitive(FieldInfo(isPassword = true, editable = true, text = "hunter2")))
    }

    @Test fun inputTypePasswordVariationsAreSecret() {
        assertTrue(Sensitive.inputTypeIsSecret(0x81))   // text | password
        assertTrue(Sensitive.inputTypeIsSecret(0x91))   // text | visible password
        assertTrue(Sensitive.inputTypeIsSecret(0xe1))   // text | web password
        assertTrue(Sensitive.inputTypeIsSecret(0x12))   // number | password (PIN pad)
        assertFalse(Sensitive.inputTypeIsSecret(0x01))
        assertFalse(Sensitive.inputTypeIsSecret(0x02))
        assertFalse(Sensitive.inputTypeIsSecret(0x21))  // email
    }

    @Test fun fieldWordsMarkCardAndCodeFields() {
        for (hint in listOf("Card number", "CVV", "Security code", "Expiry date", "One-time code", "OTP", "Verification code", "Enter your PIN", "2FA code", "SSN", "IBAN", "Recovery phrase", "Account number"))
            assertTrue("$hint should be sensitive", Sensitive.isSensitive(FieldInfo(editable = true, hint = hint)))
    }

    @Test fun viewIdsWithSecretWordsAreCaught() {
        assertTrue(Sensitive.isSensitive(FieldInfo(editable = true, viewId = "com.bank:id/card_number_input")))
        assertTrue(Sensitive.isSensitive(FieldInfo(editable = true, viewId = "com.x:id/otpCode")))
        assertTrue(Sensitive.isSensitive(FieldInfo(editable = true, viewId = "com.x:id/etPassword")))
    }

    @Test fun autofillHintsAreCaught() {
        assertTrue(Sensitive.isSensitive(FieldInfo(editable = true, autofillHints = listOf("creditCardNumber"))))
        assertTrue(Sensitive.isSensitive(FieldInfo(editable = true, autofillHints = listOf("smsOTPCode"))))
        assertTrue(Sensitive.isSensitive(FieldInfo(editable = true, autofillHints = listOf("password"))))
    }

    @Test fun ordinaryFieldsAreNotSensitive() {
        assertFalse(Sensitive.isSensitive(FieldInfo(editable = true, hint = "Search")))
        assertFalse(Sensitive.isSensitive(FieldInfo(editable = true, hint = "Message", text = "see you at 6")))
        assertFalse(Sensitive.isSensitive(FieldInfo(editable = false, text = "Forgot password?")))   // a link is not a field
        assertFalse(Sensitive.isSensitive(FieldInfo(editable = true, hint = "Name", viewId = "com.x:id/name")))
    }

    @Test fun cardNumbersAreCaughtByValueNotJustLabel() {
        assertTrue(Sensitive.isSensitive(FieldInfo(editable = true, text = "4242 4242 4242 4242")))
        assertTrue(Sensitive.isSensitive(FieldInfo(editable = false, text = "5555555555554444")))
        assertTrue(Sensitive.looksLikeCardNumber("4111-1111-1111-1111"))
        assertFalse(Sensitive.looksLikeCardNumber("4111 1111 1111 1112"))       // fails Luhn
        assertFalse(Sensitive.looksLikeCardNumber("1234567890"))
        assertFalse(Sensitive.looksLikeCardNumber("call 4242 4242 4242 4242"))
    }

    @Test fun oneTimeCodeMessagesAreMasked() {
        assertTrue(Sensitive.isSensitive(FieldInfo(text = "Your verification code is 482913")))
        assertTrue(Sensitive.isSensitive(FieldInfo(text = "G-123456 is your Google verification code")))
        assertFalse(Sensitive.isSensitive(FieldInfo(text = "Meeting at 14:30 in room 4")))
        assertFalse(Sensitive.isSensitive(FieldInfo(text = "Your order 123456 shipped")))
    }

    @Test fun splitIdentifierBreaksCamelAndSnake() {
        assertEquals("btn card Number", Sensitive.splitIdentifier("com.x:id/btn_cardNumber").trim())
    }

    @Test fun textThatIsACardNumberIsNeverTyped() {
        assertTrue(Sensitive.textIsSecret("4242424242424242"))
        assertFalse(Sensitive.textIsSecret("hello there"))
    }
}

class ApprovalTest {
    private fun tap(label: String, id: String = "") = Approval.classifyTap(listOf(label), id)

    @Test fun riskyLabelsNeedApproval() {
        for (l in listOf("Send", "SEND", "send", "Pay now", "Pay", "Delete", "Delete account", "Buy now", "Purchase", "Place order", "Confirm", "Confirm purchase", "Post", "Install", "Uninstall",
            "Accept", "I accept", "Allow", "Allow access", "Transfer", "Transfer funds", "Submit", "Checkout", "Check out", "Donate", "Withdraw", "Subscribe", "Remove", "Authorize", "Approve", "Sign up", "Reset"))
            assertTrue("\"$l\" should need approval", tap(l).needsApproval)
    }

    @Test fun inflectionsAndPunctuationAreStillCaught() {
        for (l in listOf("Sending...", "Send!", "(Delete)", "Payment", "Confirmation", "Installing", "Deleted", "Pay $5.00"))
            assertTrue("\"$l\" should need approval", tap(l).needsApproval)
    }

    @Test fun viewIdsAndDescriptionsCount() {
        assertTrue(tap("", "com.chat:id/btn_send").needsApproval)
        assertTrue(tap("", "com.chat:id/sendButton").needsApproval)
        assertTrue(tap("", "com.shop:id/pay_now").needsApproval)
        assertTrue(Approval.classifyTap(listOf("", "Send message"), "").needsApproval)
    }

    @Test fun harmlessControlsAreNotFlagged() {
        for (l in listOf("Search", "Back", "Menu", "Next", "Cancel", "Open", "Play", "Home", "Scroll", "Postal code", "Orders", "Bookmarks", "Settings icon", "Share"))
            assertFalse("\"$l\" should not need approval", tap(l).needsApproval)
    }

    @Test fun reasonIsPlainText() {
        assertTrue(tap("Send").reason.isNotBlank())
        assertEquals("", Risk.SAFE.reason)
    }

    @Test fun typingEndingInEnterIsRisky() {
        assertTrue(Approval.classifyType("hello\n", FieldInfo(editable = true, hint = "Message")).needsApproval)
        assertTrue(Approval.classifyType("hello\r", FieldInfo(editable = true, hint = "Search")).needsApproval)
        assertTrue(Approval.classifyType("hello\n", null).needsApproval)
        assertFalse(Approval.classifyType("hello", FieldInfo(editable = true, hint = "Message")).needsApproval)
        assertTrue(Approval.classifyType("hello\n", FieldInfo(editable = true, hint = "Message")).reason.contains("sends"))
    }

    @Test fun enterIsFineInSearchBoxesButNotInMessageFields() {
        assertFalse(Approval.classifyEnter(FieldInfo(editable = true, hint = "Search or type URL")).needsApproval)
        assertTrue(Approval.classifyEnter(FieldInfo(editable = true, hint = "Type a message")).needsApproval)
        assertTrue(Approval.classifyEnter(FieldInfo(editable = true, hint = "Add a comment")).needsApproval)
        assertTrue(Approval.classifyEnter(FieldInfo(editable = true, hint = "Name")).needsApproval)
        assertTrue(Approval.classifyEnter(null).needsApproval)
    }

    @Test fun describeNeutralisesQuotesAndFallsBackToTheId() {
        assertEquals("button \"Send\"", Approval.describe("button", "Send", ""))
        assertEquals("button \"say 'hi'\"", Approval.describe("button", "say \"hi\"", ""))
        assertEquals("button (btn_send)", Approval.describe("button", "", "com.x:id/btn_send"))
        assertEquals("button", Approval.describe("button", "", ""))
    }

    @Test fun fingerprintIsStableAndSensitiveToTheElement() {
        val a = Approval.fingerprint("com.x", "button", "Send", "com.x:id/send", 0)
        assertEquals(a, Approval.fingerprint("com.x", "button", "Send", "com.x:id/send", 0))
        assertEquals(a, Approval.fingerprint("COM.X", "button", "  send ", "send", 0))      // case, spacing and the id prefix are normalised
        assertEquals(Approval.fingerprint("com.x", "button", "Send", "com.x:id/send", 0), Approval.fingerprint("com.x", "button", "send", "com.x:id/send", 0))
        assertNotEquals(a, Approval.fingerprint("com.x", "button", "Send", "com.x:id/send", 1))
        assertNotEquals(a, Approval.fingerprint("com.x", "button", "Delete", "com.x:id/send", 0))
        assertNotEquals(a, Approval.fingerprint("com.y", "button", "Send", "com.x:id/send", 0))
        assertEquals(8, a.length)
        assertTrue(a.all { it in "0123456789abcdef" })
    }
}

class UrlGuardTest {
    private fun ok(u: String) = assertTrue("$u should be allowed", UrlGuard.isPublicHttp(u))
    private fun no(u: String) = assertFalse("$u should be refused", UrlGuard.isPublicHttp(u))

    @Test fun publicHttpAndHttpsAreAllowed() {
        ok("https://example.com")
        ok("http://example.com/path?q=1#x")
        ok("https://www.wikipedia.org/wiki/Cat")
        ok("https://example.com:8443/x")
        ok("HTTPS://EXAMPLE.COM")
        ok("https://8.8.8.8/")
        ok("https://sub.domain.example.co.uk/a%20b")
    }

    @Test fun otherSchemesAreRefused() {
        for (u in listOf("javascript:alert(1)", "JavaScript:alert(1)", "file:///etc/passwd", "file://localhost/etc/passwd", "ftp://example.com", "content://x/y", "intent://x#Intent;end",
            "data:text/html,<script>1</script>", "tel:123", "market://details?id=x", "sms:123", "about:blank", "example.com", "//example.com", "", "   "))
            no(u)
    }

    @Test fun smugglingTheSchemeIsRefused() {
        no(" javascript:alert(1)")
        no("java\nscript:alert(1)")
        no("\thttps://example.com@evil")
        no("https://exa mple.com")
        no("https://example.com/\u0000")
        no("http:\\\\example.com")
    }

    @Test fun loopbackAndPrivateAddressesAreRefused() {
        for (u in listOf("http://127.0.0.1", "http://127.0.0.1:8080/admin", "http://localhost", "http://localhost:3000", "https://LOCALHOST", "http://0.0.0.0", "http://10.0.0.5", "http://172.16.0.1",
            "http://172.31.255.255", "http://192.168.1.1", "http://169.254.169.254/latest/meta-data", "http://100.64.0.1", "http://224.0.0.1", "http://255.255.255.255", "http://127.1",
            "http://printer.local", "http://nas.home.arpa", "http://router.lan", "http://app.internal", "http://foo.localhost", "http://intranet", "http://server/", "http://127.0.0.1.nip.io", "http://a.b.sslip.io"))
            no(u)
    }

    @Test fun exoticIpNotationsAreRefused() {
        for (u in listOf("http://2130706433", "http://0x7f000001", "http://0x7f.0.0.1", "http://0177.0.0.1", "http://017700000001", "http://127.0.1", "http://0.0.0.0", "http://3232235777",
            "http://1.2.3.4.5", "http://999.1.1.1", "http://256.256.256.256"))
            no(u)
    }

    @Test fun ipv6LiteralsAreRefused() {
        for (u in listOf("http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://[fe80::1]/", "http://[fc00::1]/", "http://[2001:db8::1]/")) no(u)
    }

    @Test fun credentialsInTheAddressAreRefused() {
        no("http://user:pass@example.com")
        no("https://user@example.com")
        no("https://example.com@127.0.0.1/")
        no("https://example.com:80@evil.com")
        no("https://trusted.com%40evil.com@127.0.0.1")
    }

    @Test fun badPortsAndHostsAreRefused() {
        no("https://example.com:0")
        no("https://example.com:99999")
        no("https://example.com:abc")
        no("https://-bad-.com")
        no("https://exa_mple.com")
        no("https://")
        no("https:///path")
        no("https://example.com:80:80")
    }

    @Test fun overlongAddressesAreRefused() { no("https://example.com/" + "a".repeat(2100)) }

    @Test fun verdictCarriesAReasonOnlyWhenRefused() {
        assertTrue(UrlGuard.check("http://127.0.0.1").reason.isNotBlank())
        assertEquals("", UrlGuard.check("https://example.com").reason)
        assertEquals("https://example.com", UrlGuard.check("  https://example.com  ").url)
        assertNull(null as String?)
    }

    @Test fun looseIpv4ParsingMatchesInetAton() {
        assertEquals(0x7f000001L, UrlGuard.parseIpv4Loose("127.0.0.1"))
        assertEquals(0x7f000001L, UrlGuard.parseIpv4Loose("2130706433"))
        assertEquals(0x7f000001L, UrlGuard.parseIpv4Loose("127.1"))
        assertEquals(0x7f000001L, UrlGuard.parseIpv4Loose("0x7f.1"))
        assertNotNull(UrlGuard.parseIpv4Loose("8.8.8.8"))
        assertNull(UrlGuard.parseIpv4Loose("1.2.3.4.5"))
        assertNull(UrlGuard.parseIpv4Loose("300.1.1.1"))
    }
}
