package app.nori.companion.core

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

private fun raw(pkg: String = "com.chat.app", title: String = "Sam", text: String = "are you free tonight?", category: String = "msg", ongoing: Boolean = false, group: Boolean = false, secret: Boolean = false, ts: Long = 1_000L) =
    RawNotification(pkg, "Chat", title, text, "", category, "k-$pkg", ts, ongoing, group, secret)

private fun slot(server: CaptureLevel = CaptureLevel.ALL, phone: CaptureLevel = CaptureLevel.ALL, apps: List<String> = emptyList(), hidden: List<String> = emptyList(), active: Boolean = true,
                 hideCodes: Boolean = true, sms: Boolean = false, smsPhone: Boolean = false, smsShort: Boolean = false) =
    SlotCapture(0, active, server, phone, apps, hidden, hideCodes, sms, smsPhone, smsShort)

class CaptureLevelTests {
    @Test fun defaultsAndTolerantParsing() {
        assertEquals(CaptureLevel.OFF, CaptureLevel.parse(null))
        assertEquals(CaptureLevel.OFF, CaptureLevel.parse(""))
        assertEquals(CaptureLevel.OFF, CaptureLevel.parse("true"))
        assertEquals(CaptureLevel.OFF, CaptureLevel.parse("everything"))
        assertEquals(CaptureLevel.APPS, CaptureLevel.parse("apps"))
        assertEquals(CaptureLevel.ALL, CaptureLevel.parse(" ALL "))
    }

    @Test fun effectiveIsTheLowerOfTheTwoKeys() {
        val L = CaptureLevel.values()
        for (a in L) for (b in L) assertEquals("$a/$b", L[minOf(a.ordinal, b.ordinal)], Capture.effective(a, b))
        assertEquals(CaptureLevel.OFF, Capture.effective(CaptureLevel.ALL, CaptureLevel.OFF))
        assertEquals(CaptureLevel.APPS, Capture.effective(CaptureLevel.ALL, CaptureLevel.APPS))
        assertEquals(CaptureLevel.APPS, Capture.effective(CaptureLevel.APPS, CaptureLevel.ALL))
    }

    @Test fun anOldServerThatSendsNothingMeansOff() {
        val s = DeviceSettings.parse("""{"notifications":true,"health":true,"hidden_apps":["com.bank"]}""")
        assertEquals(CaptureLevel.OFF, s.capture); assertTrue(s.captureApps.isEmpty()); assertTrue(s.hideCodes); assertFalse(s.captureSms); assertFalse(s.captureSmsShort)
        assertEquals(CaptureLevel.OFF, DeviceSettings.parse(null).capture)
        assertEquals(CaptureLevel.OFF, DeviceSettings.parse("not json").capture)
        assertEquals(CaptureLevel.OFF, DeviceSettings.parse("""{"capture":true}""").capture)        // a boolean is not a level
    }

    @Test fun settingsRoundTripAndTheChosenAppListIsExactPackagesOnly() {
        val s = DeviceSettings.parse("""{"capture":"apps","capture_apps":["com.ring.app","*","com.bank.*","Com.Door.Cam","  ","nodots","a b.c"],"hide_codes":false,"capture_sms":true,"capture_sms_short":true}""")
        assertEquals(CaptureLevel.APPS, s.capture)
        assertEquals(listOf("com.ring.app", "com.door.cam"), s.captureApps)
        assertFalse(s.hideCodes); assertTrue(s.captureSms); assertTrue(s.captureSmsShort)
        val back = DeviceSettings.parse(s.toJson().toString())
        assertEquals(s.capture, back.capture); assertEquals(s.captureApps, back.captureApps); assertEquals(s.hideCodes, back.hideCodes); assertEquals(s.captureSms, back.captureSms); assertEquals(s.captureSmsShort, back.captureSmsShort)
    }

    @Test fun theCapsFlagsArePerConnection() {
        assertTrue(Capture.capsFlag(CaptureLevel.APPS, true)); assertTrue(Capture.capsFlag(CaptureLevel.ALL, true))
        assertFalse(Capture.capsFlag(CaptureLevel.OFF, true)); assertFalse(Capture.capsFlag(CaptureLevel.ALL, false))
        assertTrue(Capture.smsCapsFlag(true, true)); assertFalse(Capture.smsCapsFlag(true, false)); assertFalse(Capture.smsCapsFlag(false, true))
        val j: JSONObject = Caps(true, true, false, false, false, false, false, capture = true, sms = false).toJson()
        assertTrue(j.getBoolean("capture")); assertFalse(j.getBoolean("sms"))
        assertFalse(Caps(true, true, false, false, false, false, false).toJson().getBoolean("capture"))
        val a = Caps(true, true, false, false, false, false, false)
        assertTrue(a.copy(capture = true).capture); assertFalse(a.capture)         // one connection's flag never leaks into another's
    }

    @Test fun wordsSayWhatIsActuallyShared() {
        fun w(s: CaptureLevel, p: CaptureLevel, access: Boolean, n: Int = 0) = CaptureWords.effective("Iron", s, p, access, n)
        assertTrue(w(CaptureLevel.ALL, CaptureLevel.OFF, true).contains("not turned this on"))
        assertTrue(w(CaptureLevel.OFF, CaptureLevel.ALL, true).contains("server has not switched"))
        assertTrue(w(CaptureLevel.ALL, CaptureLevel.ALL, false).contains("Notification access"))
        assertTrue(w(CaptureLevel.APPS, CaptureLevel.ALL, true, 0).contains("not listed any"))
        assertTrue(w(CaptureLevel.APPS, CaptureLevel.ALL, true, 2).contains("2 chosen apps"))
        assertTrue(w(CaptureLevel.ALL, CaptureLevel.ALL, true).contains("every notification"))
        assertTrue(CaptureWords.sms("Iron", true, true, false).contains("permission"))
        assertTrue(CaptureWords.sms("Iron", true, true, true).contains("new incoming texts"))
    }
}

class CaptureFilterTests {
    @Test fun nothingStartsWhileEverythingIsOffOrPaused() {
        assertFalse(Capture.anyWanted(listOf(slot(phone = CaptureLevel.OFF))))
        assertFalse(Capture.anyWanted(listOf(slot(server = CaptureLevel.OFF))))
        assertFalse(Capture.anyWanted(listOf(slot(active = false))))                 // paused, not paired, or told to re-pair
        assertTrue(Capture.anyWanted(listOf(slot(), slot(phone = CaptureLevel.OFF))))
        assertNull(Capture.forSlot(raw(), slot(phone = CaptureLevel.OFF)))
        assertNull(Capture.forSlot(raw(), slot(server = CaptureLevel.OFF)))
        assertNull(Capture.forSlot(raw(), slot(active = false)))
    }

    @Test fun chosenAppsNeverShareAnythingOutsideTheList() {
        val s = slot(phone = CaptureLevel.APPS, apps = listOf("com.ring.app"))
        assertNotNull(Capture.forSlot(raw(pkg = "com.ring.app", text = "Motion at the front door"), s))
        assertNull(Capture.forSlot(raw(pkg = "com.chat.app"), s))
        assertNull(Capture.forSlot(raw(pkg = "com.chat.app"), slot(phone = CaptureLevel.APPS, apps = emptyList())))
        assertNull(Capture.forSlot(raw(pkg = "com.ring.app"), slot(server = CaptureLevel.APPS, phone = CaptureLevel.ALL, apps = listOf("com.other"))))
        assertNotNull(Capture.forSlot(raw(pkg = "com.ring.app"), slot(server = CaptureLevel.APPS, phone = CaptureLevel.ALL, apps = listOf("com.ring.app"))))
        assertNotNull(Capture.forSlot(raw(pkg = "COM.Ring.App"), s))
    }

    @Test fun everythingStillExcludesPrivateAppsAndTheHiddenList() {
        val all = slot(hidden = listOf("com.bank.app", "com.family.*"))
        assertNotNull(Capture.forSlot(raw(), all))
        for (pkg in listOf("com.android.settings", "app.nori.companion", "com.x8bit.bitwarden", "com.google.android.apps.walletnfcrel", "com.android.systemui", "com.android.vending", "com.google.android.apps.authenticator2"))
            assertNull(pkg, Capture.forSlot(raw(pkg = pkg), all))
        assertNull(Capture.forSlot(raw(pkg = "com.bank.app"), all))
        assertNull(Capture.forSlot(raw(pkg = "com.family.chat"), all))
        assertNull("even a server-listed chosen app cannot be a private one", Capture.forSlot(raw(pkg = "com.x8bit.bitwarden"), slot(server = CaptureLevel.APPS, phone = CaptureLevel.APPS, apps = listOf("com.x8bit.bitwarden"))))
    }

    @Test fun noiseOngoingSecretAndEmptyAreSkipped() {
        val all = slot()
        assertNull(Capture.forSlot(raw(ongoing = true), all))
        assertNull(Capture.forSlot(raw(group = true), all))
        assertNull(Capture.forSlot(raw(secret = true), all))
        for (cat in listOf("progress", "transport", "alarm", "service", "status", "sys")) assertNull(cat, Capture.forSlot(raw(category = cat), all))
        assertNull(Capture.forSlot(raw(title = " ", text = ""), all))
        assertNotNull(Capture.forSlot(raw(category = "email"), all))
        assertNotNull(Capture.forSlot(raw(category = "call", title = "Incoming call", text = ""), all))
    }

    @Test fun itemsAreCappedCleanedAndCodesHiddenPerConnection() {
        val long = "x".repeat(900)
        val n = Capture.forSlot(raw(title = "T".repeat(300), text = long), slot())!!
        assertEquals(Capture.TITLE_MAX, n.title.length); assertEquals(Capture.TEXT_MAX, n.text.length)
        assertEquals("a b", Capture.forSlot(raw(text = "a\u0000\u0007 b"), slot())!!.text)
        assertFalse(Capture.forSlot(raw(text = "Your verification code is 482913"), slot())!!.text.contains("482913"))
        assertTrue("a server that turned hiding off gets the text as it was", Capture.forSlot(raw(text = "Your verification code is 482913"), slot(hideCodes = false))!!.text.contains("482913"))
        assertEquals("com.chat.app", n.pkg)
    }

    @Test fun chatTailIsTheLastThreeLinesWithWhoSaidThem() {
        val t = Capture.chatTail(listOf("A" to "1", "B" to "2", "" to "3", "C" to "4"))
        assertEquals("B: 2 / 3 / C: 4", t)
    }

    @Test fun theSentLogGetsLabelsNeverText() {
        val items = listOf(Notif("a.b", "Ring", "Door", "Motion", "", "", "", 1), Notif("c.d", "Messages", "Sam", "hi", "", "", "", 2), Notif("a.b", "Ring", "Door", "again", "", "", "", 3))
        val line = Capture.labelsLine(items)
        assertEquals("Ring, Messages", line)
        assertFalse(line.contains("Motion") || line.contains("Sam") || line.contains("hi"))
        assertTrue(Capture.labelsLine((1..7).map { Notif("p$it.x", "App$it", "", "t", "", "", "", 1) }).endsWith("+3 more"))
    }

    @Test fun theListOfRecentAppsHasNoContentAndIsBounded() {
        val r = RecentApps()
        for (i in 1..60) r.note("com.app$i", "App $i", i.toLong())
        r.note("com.app5", "App 5", 100)
        assertEquals(Capture.MAX_RECENT_APPS, r.all().size)
        assertEquals("com.app5", r.all()[0].first)
        assertEquals(1, r.all().count { it.first == "com.app5" })
        val json = r.serialize()
        assertFalse(json.contains("\"text\"") || json.contains("\"title\""))
        assertEquals(r.all(), RecentApps.parse(json).all())
        assertTrue(RecentApps.parse("garbage").all().isEmpty())
    }
}

class RedactTests {
    @Test fun codesAreHiddenWhereTheTextTalksAboutACode() {
        assertFalse(Redact.codes("Your verification code is 482913").contains("482913"))
        assertFalse(Redact.codes("G-482913 is your Google verification code").contains("482913"))
        assertFalse(Redact.codes("Use 123-456 to log in").contains("123-456"))
        assertFalse(Redact.codes("Your 2FA code: 5521").contains("5521"))
        assertFalse(Redact.codes("Your PIN is 90210, do not share").contains("90210"))
    }

    @Test fun aCodeBesidePunctuationIsStillHidden() {
        for (t in listOf("Your code is 482913.", "code:482913", "Code 482913, valid for 10 minutes", "(482913) is your verification code", "Verification code: 482913!", "OTP=482913;"))
            assertFalse(t, Redact.codes(t).contains("482913"))
        assertEquals("Meet at 10:3045 near the pin drop", Redact.codes("Meet at 10:3045 near the pin drop"))        // digits after a colon belong to a time, not a code
    }

    @Test fun ordinaryNumbersSurvive() {
        for (t in listOf("See you at 7:30 pm, table for 4", "Order 98765 has shipped", "Your total is \$4500.00 for the rental", "Battery at 15%, 2024 was fun", "Dinner at 1830 tonight?", "Call me at 5551234567", "Meet at 6.30 near gate 4421")) {
            assertEquals(t, Redact.codes(t))
        }
        assertEquals("Total \$4500, security deposit refunded", Redact.codes("Total \$4500, security deposit refunded"))
    }

    @Test fun emptyAndPlainTextAreUntouched() {
        assertEquals("", Redact.codes(""))
        assertEquals("hello there", Redact.codes("hello there"))
    }
}

class CaptureOutboxTests {
    private fun n(i: Int, ts: Long = 1_000_000L) = Notif("com.a.b", "A", "t$i", "x$i", "", "", "k$i", ts)

    @Test fun dedupesBoundsAndDropsOldestFirst() {
        val o = CaptureOutbox()
        o.add(n(1)); o.add(n(1)); assertEquals(1, o.size())
        for (i in 2..260) o.add(n(i))
        assertEquals(Capture.MAX_QUEUED, o.size())
        assertEquals("t61", o.peek(1)[0].title)                                       // 1..60 were dropped
        assertEquals(Capture.MAX_PER_CALL, o.peek(500).size)
        o.drop(60); assertEquals(Capture.MAX_QUEUED - 60, o.size())
        o.drop(10_000); assertEquals(0, o.size())
    }

    @Test fun anythingOlderThanSixHoursIsDroppedNotSentLate() {
        val now = 100L * 3_600_000L
        val o = CaptureOutbox(listOf(n(1, now - 7 * 3_600_000L), n(2, now - 5 * 3_600_000L), n(3, now)))
        o.prune(now)
        assertEquals(listOf("t2", "t3"), o.peek().map { it.title })
    }

    @Test fun clearRetainAndTheRoundTrip() {
        val o = CaptureOutbox(listOf(n(1), Notif("sms", "Text message", "+15551234567", "hi", "", "sms", "", 5), n(2)))
        o.retain { it.pkg != Capture.SMS_PKG }
        assertEquals(listOf("t1", "t2"), o.peek().map { it.title })
        val back = CaptureOutbox.parse(o.serialize())
        assertEquals(o.peek(), back.peek())
        val body = o.body()
        assertEquals(2, body.getJSONArray("items").length())
        for (k in listOf("pkg", "app", "title", "text", "sub", "category", "key", "ts")) assertTrue(k, body.getJSONArray("items").getJSONObject(0).has(k))
        o.clear(); assertEquals(0, o.size())
        assertEquals(0, CaptureOutbox.parse("garbage").size()); assertEquals(0, CaptureOutbox.parse(null).size()); assertEquals(0, CaptureOutbox.parse("").size())
    }
}

class SmsCaptureTests {
    private fun on(short: Boolean = false, hideCodes: Boolean = true) = slot(server = CaptureLevel.OFF, phone = CaptureLevel.OFF, sms = true, smsPhone = true, smsShort = short, hideCodes = hideCodes)

    @Test fun effectiveNeedsEveryKey() {
        val base = on()
        assertTrue(Capture.smsEffective(base, granted = true))
        assertFalse("permission not granted", Capture.smsEffective(base, granted = false))
        assertFalse("server off", Capture.smsEffective(slot(sms = false, smsPhone = true), true))
        assertFalse("owner's switch off", Capture.smsEffective(slot(sms = true, smsPhone = false), true))
        assertFalse("paused / unpaired / needs re-pair", Capture.smsEffective(slot(active = false, sms = true, smsPhone = true), true))
        assertFalse(Capture.smsEffective(slot(), true))                                // notifications at ALL do not turn texts on
        assertTrue(Capture.anyWantedSms(listOf(slot(), base), true)); assertFalse(Capture.anyWantedSms(listOf(slot(), base), false))
    }

    @Test fun textsAndNotificationsAreIndependent() {
        val textsOnly = on()
        assertNotNull(Capture.smsFor("+15551234567", "hello", 5L, textsOnly, true))
        assertNull("notification level is off", Capture.forSlot(raw(), textsOnly))
        val notifsOnly = slot()
        assertNotNull(Capture.forSlot(raw(), notifsOnly))
        assertNull("texts are off", Capture.smsFor("+15551234567", "hello", 5L, notifsOnly, true))
    }

    @Test fun shortCodesAndSenderNamesAreSkippedUnlessTheServerAllows() {
        for (a in listOf("12345", "123456", "32665", "MYBANK", "Bank-Alert", "", "  ", "AD-HDFCBK")) {
            assertTrue("$a is a short sender", Capture.isShortSender(a))
            assertNull(a, Capture.smsFor(a, "your balance is low", 1L, on(), true))
            assertNotNull(a, Capture.smsFor(a, "your balance is low", 1L, on(short = true), true))
        }
        for (a in listOf("+15551234567", "5551234567", "(555) 123-4567", "+44 7700 900123", "1234567")) {
            assertFalse("$a is an ordinary number", Capture.isShortSender(a))
            assertNotNull(a, Capture.smsFor(a, "hi", 1L, on(), true))
        }
    }

    @Test fun itemShapeIsTheSameAsANotificationAndCapped() {
        val n = Capture.smsFor("+15551234567", "a".repeat(900), 1792000000000L, on(), true)!!
        assertEquals("sms", n.pkg); assertEquals("Text message", n.app); assertEquals("sms", n.category); assertEquals("", n.sub); assertEquals("", n.key)
        assertEquals("+15551234567", n.title); assertEquals(Capture.TEXT_MAX, n.text.length); assertEquals(1792000000000L, n.ts)
        assertEquals(160, Capture.smsFor("1".repeat(300), "hi", 1L, on(), true)!!.title.length.coerceAtMost(160))
        assertNull("an empty text is not shared", Capture.smsFor("+15551234567", "  ", 1L, on(), true))
    }

    @Test fun codesInTextsAreHiddenByDefault() {
        assertFalse(Capture.smsFor("+15551234567", "Your Venmo code is 482913", 1L, on(), true)!!.text.contains("482913"))
        assertTrue(Capture.smsFor("+15551234567", "Your Venmo code is 482913", 1L, on(hideCodes = false), true)!!.text.contains("482913"))
        assertEquals("see you at 7:30", Capture.smsFor("+15551234567", "see you at 7:30", 1L, on(), true)!!.text)
    }

    @Test fun theHiddenAppListDoesNotApplyToTexts() {
        val s = SlotCapture(0, true, CaptureLevel.ALL, CaptureLevel.ALL, emptyList(), listOf("sms", "*"), true, true, true, false)
        assertNotNull(Capture.smsFor("+15551234567", "hi", 1L, s, true))
    }

    @Test fun theMessagingAppsOwnNotificationIsStillSharedWhenTextsAreOn() {
        // RCS chats reach the phone only as the messaging app's notification, so it must not be suppressed when SMS is on; the server drops the second copy of a plain SMS.
        val s = SlotCapture(0, true, CaptureLevel.ALL, CaptureLevel.ALL, emptyList(), emptyList(), true, true, true, false)
        val n = Capture.forSlot(raw(pkg = "com.google.android.apps.messaging", title = "Mom", text = "dinner at 6?"), s)
        assertNotNull(n); assertEquals("com.google.android.apps.messaging", n!!.pkg)
        val chosen = SlotCapture(0, true, CaptureLevel.APPS, CaptureLevel.APPS, listOf("com.google.android.apps.messaging"), emptyList(), true, true, true, false)
        assertNotNull(Capture.forSlot(raw(pkg = "com.google.android.apps.messaging"), chosen))
    }
}
