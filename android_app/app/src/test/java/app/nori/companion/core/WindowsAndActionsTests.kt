package app.nori.companion.core

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class WindowsTest {
    private val now = 1_800_000_000_000L

    @Test fun onlyTheThreeLengthsAreAllowed() {
        assertEquals(now + 15 * 60_000L, Windows.open(now, 15))
        assertEquals(now + 60 * 60_000L, Windows.open(now, 60))
        assertEquals(now + 180 * 60_000L, Windows.open(now, 180))
        for (m in listOf(0, 1, 5, 30, 120, 181, 600, -15, 99999)) assertNull("$m minutes must be refused", Windows.open(now, m))
    }

    @Test fun aWindowIsOpenOnlyBeforeItsEnd() {
        val until = now + 60_000L
        assertTrue(Windows.isOpen(until, now))
        assertTrue(Windows.isOpen(until, until - 1))
        assertFalse(Windows.isOpen(until, until))             // expired exactly at the end
        assertFalse(Windows.isOpen(until, until + 1))
        assertFalse(Windows.isOpen(0, now))                   // never opened
        assertFalse(Windows.isOpen(-5, now))
    }

    @Test fun anImpossiblyLongWindowIsTreatedAsClosed() {
        // the clock was moved back (or the value was tampered with): a window can never be more than three hours (plus slack) away
        assertFalse(Windows.isOpen(now + 4 * 3600_000L, now))
        assertFalse(Windows.isOpen(Long.MAX_VALUE, now))
        assertTrue(Windows.isOpen(now + 3 * 3600_000L, now))
    }

    @Test fun expiredWindowsReportZeroAndOpenOnesTheirEnd() {
        assertEquals(0L, Windows.reportedUntil(now - 1, now))
        assertEquals(0L, Windows.reportedUntil(0, now))
        assertEquals(now + 5000, Windows.reportedUntil(now + 5000, now))
        assertEquals(0L, Windows.remainingMs(now - 1, now))
        assertEquals(5000L, Windows.remainingMs(now + 5000, now))
    }

    @Test fun countdownAndLabels() {
        assertEquals("14:59", Windows.countdown(14 * 60_000L + 59_000L))
        assertEquals("1:00:00", Windows.countdown(3600_000L))
        assertEquals("0:00", Windows.countdown(-5))
        assertEquals("15 min", Windows.label(15))
        assertEquals("1 h", Windows.label(60))
        assertEquals("3 h", Windows.label(180))
    }
}

class HourlyCapTest {
    private val t0 = 1_800_000_000_000L

    @Test fun allowsExactlyTheLimitPerHour() {
        val cap = HourlyCap()
        repeat(3) { assertTrue(cap.tryTake(t0 + it, 3)) }
        assertFalse(cap.tryTake(t0 + 10, 3))
        assertEquals(3, cap.used(t0 + 10))
    }

    @Test fun theBudgetComesBackAfterAnHour() {
        val cap = HourlyCap()
        repeat(2) { assertTrue(cap.tryTake(t0, 2)) }
        assertFalse(cap.tryTake(t0 + 59 * 60_000L, 2))
        assertTrue(cap.tryTake(t0 + 3600_000L + 1, 2))
    }

    @Test fun exhaustionIsExactNotRounded() {
        val cap = HourlyCap()
        assertTrue(cap.tryTake(t0, 1))
        assertFalse(cap.tryTake(t0 + 3599_999L, 1))
        assertTrue(cap.tryTake(t0 + 3600_000L, 1))
    }

    @Test fun zeroOrNegativeLimitAllowsNothing() {
        val cap = HourlyCap()
        assertFalse(cap.tryTake(t0, 0))
        assertFalse(cap.tryTake(t0, -5))
        assertFalse(cap.wouldAllow(t0, 0))
    }

    @Test fun wouldAllowDoesNotConsume() {
        val cap = HourlyCap()
        repeat(5) { assertTrue(cap.wouldAllow(t0, 1)) }
        assertEquals(0, cap.used(t0))
    }

    @Test fun lowerLimitAppliesToHistory() {
        val cap = HourlyCap()
        repeat(5) { cap.tryTake(t0, 10) }
        assertFalse(cap.tryTake(t0, 5))     // the server lowered the limit: the past counts
        assertTrue(cap.tryTake(t0, 6))
    }

    @Test fun survivesARestartThroughSerialisation() {
        val cap = HourlyCap()
        repeat(4) { cap.tryTake(t0 + it, 10) }
        val again = HourlyCap.parse(cap.serialize())
        assertEquals(4, again.used(t0 + 10))
        assertFalse(again.tryTake(t0 + 10, 4))
    }

    @Test fun clockMovedBackDoesNotRefillTheBudget() {
        val cap = HourlyCap()
        repeat(2) { cap.tryTake(t0 + 10 * 3600_000L, 2) }       // stamps are "in the future" for a clock that moved back
        assertFalse(cap.tryTake(t0, 2))
    }

    @Test fun garbageStateIsIgnored() {
        assertEquals(0, HourlyCap.parse("abc,,;;").used(t0))
        assertEquals(0, HourlyCap.parse(null).used(t0))
        assertEquals(1, HourlyCap.parse("abc,${t0}").used(t0))
    }
}

class ActionArgsTest {
    private fun j(s: String) = JSONObject(s)

    @Test fun timerNeedsPositiveSecondsUpToADay() {
        assertEquals(300, ActionArgs.timer(j("""{"seconds":300,"label":"pasta"}""")).value!!.seconds)
        assertEquals("pasta", ActionArgs.timer(j("""{"seconds":300,"label":"pasta"}""")).value!!.label)
        for (bad in listOf("""{}""", """{"seconds":0}""", """{"seconds":-5}""", """{"seconds":86401}""", """{"seconds":"abc"}""", """{"seconds":1e12}"""))
            assertNotNull("$bad must be refused", ActionArgs.timer(j(bad)).error)
        assertNotNull(ActionArgs.timer(null).error)
    }

    @Test fun labelsAreCleanedAndCapped() {
        val t = ActionArgs.timer(j("""{"seconds":10,"label":"  hi\u0007 there  ${"x".repeat(100)}"}""")).value!!
        assertTrue(t.label.length <= 60)
        assertFalse(t.label.any { it.code < 0x20 })
    }

    @Test fun alarmValidatesHourMinuteAndDays() {
        val a = ActionArgs.alarm(j("""{"hour":7,"minute":30,"label":"gym","days":[2,"wed","Friday"]}""")).value!!
        assertEquals(7, a.hour); assertEquals(30, a.minute); assertEquals(listOf(2, 4, 6), a.days)
        for (bad in listOf("""{"hour":24,"minute":0}""", """{"hour":-1,"minute":0}""", """{"hour":7,"minute":60}""", """{"hour":7}""", """{"minute":7}""", """{"hour":7,"minute":0,"days":[8]}""", """{"hour":7,"minute":0,"days":["someday"]}"""))
            assertNotNull("$bad must be refused", ActionArgs.alarm(j(bad)).error)
        assertTrue(ActionArgs.alarm(j("""{"hour":0,"minute":0}""")).value!!.days.isEmpty())
    }

    @Test fun navigateBuildsAnEncodedGeoUri() {
        assertEquals("geo:0,0?q=Cafe%20Roma%2C%20Rome", ActionArgs.navigateUri(j("""{"query":"Cafe Roma, Rome"}""")).value)
        assertNotNull(ActionArgs.navigateUri(j("""{"query":"  "}""")).error)
        assertNotNull(ActionArgs.navigateUri(j("""{}""")).error)
        assertNotNull(ActionArgs.navigateUri(j("""{"query":"${"a".repeat(300)}"}""")).error)
        // injection attempts stay inside the q= value
        assertFalse(ActionArgs.navigateUri(j("""{"query":"x&daddr=evil#frag"}""")).value!!.contains("&daddr"))
    }

    @Test fun openAppNeedsAValidPackageOrAName() {
        assertEquals("com.google.android.apps.maps", ActionArgs.appTarget(j("""{"package":"com.google.android.apps.maps"}""")).value!!.pkg)
        assertEquals("Maps", ActionArgs.appTarget(j("""{"name":"Maps"}""")).value!!.name)
        assertNotNull(ActionArgs.appTarget(j("""{"package":"not a package"}""")).error)
        assertNotNull(ActionArgs.appTarget(j("""{}""")).error)
        assertNotNull(ActionArgs.appTarget(null).error)
    }

    @Test fun nameResolutionNeverGuessesBetweenSeveral() {
        val apps = listOf("com.a.maps" to "Maps", "com.b.maps2" to "Maps Go", "com.c.clock" to "Clock", "com.d.clock2" to "World Clock")
        assertEquals("com.a.maps", ActionArgs.resolveByName("maps", apps).value)
        assertNotNull(ActionArgs.resolveByName("clo", apps).error)         // two partial matches
        assertEquals("com.c.clock", ActionArgs.resolveByName("Clock", apps).value)
        assertNotNull(ActionArgs.resolveByName("Camera", apps).error)
    }
}

class CommandGateTest {
    private val now = 1_800_000_000_000L
    private fun owner(all: Boolean = true) = OwnerSwitches(notifications = true, share = true, health = all, places = all, actions = all, screen = all, control = all)
    private fun server(all: Boolean = true) = DeviceSettings(notifications = true, health = all, places = all, actions = all, control = all, screen = all)
    private fun state(
        paused: Boolean = false, server: DeviceSettings = server(), owner: OwnerSwitches = owner(), interactUntil: Long = now + 60_000, shareUntil: Long = now + 60_000,
        a11y: Boolean = true, projection: Boolean = true,
    ) = GateState(now, paused, server, owner, interactUntil, shareUntil, a11y, projection)

    @Test fun everythingAllowedWhenBothKeysAndWindowsAreOn() {
        for (a in CommandGate.ALL) assertNull("$a should pass", CommandGate.check(a, state()))
    }

    @Test fun unknownActionsAreRefusedWithAReason() {
        val e = CommandGate.check("format_phone", state())
        assertNotNull(e); assertTrue(e!!.isNotBlank())
        assertNotNull(CommandGate.check("", state()))
    }

    @Test fun pausedRefusesEverything() {
        for (a in CommandGate.ALL) assertNotNull("$a while paused", CommandGate.check(a, state(paused = true)))
    }

    @Test fun serverSwitchOffRefusesEvenIfTheOwnerAllowed() {
        assertNotNull(CommandGate.check("open_url", state(server = server().copy(actions = false))))
        assertNotNull(CommandGate.check("read_screen", state(server = server().copy(control = false))))
        assertNotNull(CommandGate.check("interact", state(server = server().copy(control = false))))
        assertNotNull(CommandGate.check("screenshot", state(server = server().copy(screen = false))))
    }

    @Test fun ownerSwitchOffRefusesEvenIfTheServerAllowed() {
        assertNotNull(CommandGate.check("open_url", state(owner = owner().copy(actions = false))))
        assertNotNull(CommandGate.check("set_timer", state(owner = owner().copy(actions = false))))
        assertNotNull(CommandGate.check("read_screen", state(owner = owner().copy(control = false))))
        assertNotNull(CommandGate.check("screenshot", state(owner = owner().copy(screen = false))))
    }

    @Test fun controlNeedsTheInteractWindowToBeOpen() {
        for (until in listOf(0L, now - 1, now, now + 5 * 3600_000L)) {
            assertNotNull("read_screen with window end $until", CommandGate.check("read_screen", state(interactUntil = until)))
            assertNotNull("interact with window end $until", CommandGate.check("interact", state(interactUntil = until)))
        }
        // the window is the owner's: the share window does not open control
        assertNotNull(CommandGate.check("interact", state(interactUntil = 0, shareUntil = now + 60_000)))
        // and actions do not need either window
        assertNull(CommandGate.check("open_url", state(interactUntil = 0, shareUntil = 0)))
    }

    @Test fun screenshotNeedsTheShareWindowAndALiveProjection() {
        assertNotNull(CommandGate.check("screenshot", state(shareUntil = 0)))
        assertNotNull(CommandGate.check("screenshot", state(shareUntil = now - 1)))
        assertNotNull(CommandGate.check("screenshot", state(projection = false)))
        assertNotNull(CommandGate.check("screenshot", state(shareUntil = 0, interactUntil = now + 60_000)))
    }

    @Test fun controlNeedsTheAccessibilityService() {
        assertNotNull(CommandGate.check("read_screen", state(a11y = false)))
        assertNotNull(CommandGate.check("interact", state(a11y = false)))
        assertNull(CommandGate.check("open_url", state(a11y = false)))
    }

    @Test fun refusalsAreNeverBlank() {
        val states = listOf(state(paused = true), state(server = server(false)), state(owner = owner(false)), state(interactUntil = 0, shareUntil = 0), state(a11y = false), state(projection = false))
        for (st in states) for (a in CommandGate.ALL) CommandGate.check(a, st)?.let { assertTrue(it.isNotBlank()) }
    }

    @Test fun onlyChangingCommandsCountAgainstTheCap() {
        assertTrue("open_url" in CommandGate.COUNTED)
        assertTrue("set_timer" in CommandGate.COUNTED)
        assertFalse("read_screen" in CommandGate.COUNTED)
        assertFalse("screenshot" in CommandGate.COUNTED)
        assertFalse("list_apps" in CommandGate.COUNTED)
        assertEquals(120, CommandGate.interactLimit(30))
        assertEquals(0, CommandGate.interactLimit(0))
        assertEquals(0, CommandGate.interactLimit(-3))
    }

    @Test fun foregroundCheckAppliesToScreenCommandsOnly() {
        assertTrue(CommandGate.needsForegroundCheck("read_screen"))
        assertTrue(CommandGate.needsForegroundCheck("interact"))
        assertTrue(CommandGate.needsForegroundCheck("screenshot"))
        assertFalse(CommandGate.needsForegroundCheck("open_url"))
    }

    @Test fun deviceCapsReflectTheOwnerHalfAndReadiness() {
        val c = CommandGate.deviceCaps(owner(), accessibilityRunning = false, projectionReady = true, notificationsPermitted = true, healthReady = true, placesReady = false)
        assertTrue(c.notifications); assertTrue(c.share); assertTrue(c.health); assertFalse(c.places); assertTrue(c.actions); assertTrue(c.screen); assertFalse(c.control)
        val off = CommandGate.deviceCaps(OwnerSwitches(), true, true, true, true, true)
        assertTrue(off.notifications); assertFalse(off.health); assertFalse(off.places); assertFalse(off.actions); assertFalse(off.screen); assertFalse(off.control)
        val json = c.toJson()
        for (k in listOf("notifications", "share", "health", "places", "actions", "screen", "control")) assertTrue(json.has(k))
        assertEquals(7, json.length())
        assertNotNull(JSONArray())
    }
}
