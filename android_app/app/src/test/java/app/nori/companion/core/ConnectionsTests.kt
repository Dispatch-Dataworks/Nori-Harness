package app.nori.companion.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** One phone, several Nori servers: where each connection's values are kept, what they are called, where a pairing goes, and the ids that must never collide between servers. */
class ConnectionsTest {
    // ── storage names ────────────────────────────────────────────────
    @Test fun slotZeroKeepsTheNamesOlderVersionsUsed() {
        // a phone paired before connections existed must keep working untouched
        for (n in listOf("server", "token", "name", "needs_repair", "ack", "seen", "settings", "health_ledger", "place_tracker", "event_outbox", "current_place"))
            assertEquals(n, Connections.key(0, n))
    }

    @Test fun otherSlotsGetTheirOwnNamesAndNoSlotShadowsAnother() {
        val keys = (0 until Connections.MAX).map { Connections.key(it, "token") }
        assertEquals(keys.size, keys.toSet().size)
        assertEquals("c1_token", Connections.key(1, "token"))
        // a slot's prefix can never be mistaken for a plain setting of the phone
        for (s in 1 until Connections.MAX) assertTrue(Connections.key(s, "paused").startsWith("c$s" + "_"))
    }

    @Test fun onlyRealSlotsAreSlots() {
        assertTrue(Connections.isSlot(0)); assertTrue(Connections.isSlot(Connections.MAX - 1))
        assertFalse(Connections.isSlot(-1)); assertFalse(Connections.isSlot(Connections.MAX))
    }

    // ── names ────────────────────────────────────────────────────────
    @Test fun aConnectionIsNamedAfterItsHostUntilTheOwnerNamesIt() {
        assertEquals("Nori", Connections.defaultLabel("nori.example.com"))
        assertEquals("Iron", Connections.defaultLabel("IRON.home.example.com"))
        assertEquals("192.168.1.20", Connections.defaultLabel("192.168.1.20"))
        assertEquals("Nori", Connections.defaultLabel(""))
        assertTrue(Connections.defaultLabel("averyveryveryverylonghostnamehere.example.com").length <= 20)
    }

    @Test fun twoConnectionsThatWouldReadTheSameGetTheirHostAdded() {
        val names = Connections.displayNames(listOf(Connections.Entry(0, "Nori", "nori.a.com"), Connections.Entry(1, "Nori", "nori.b.com"), Connections.Entry(2, "Iron", "iron.a.com")))
        assertEquals("Nori (nori.a.com)", names[0])
        assertEquals("Nori (nori.b.com)", names[1])
        assertEquals("Iron", names[2])
    }

    @Test fun distinctNamesAreLeftAlone() {
        val names = Connections.displayNames(listOf(Connections.Entry(0, "Nori", "nori.a.com"), Connections.Entry(1, "Iron", "iron.a.com")))
        assertEquals(mapOf(0 to "Nori", 1 to "Iron"), names)
    }

    @Test fun aBlankLabelFallsBackToTheHostName() {
        assertEquals("Iron", Connections.displayNames(listOf(Connections.Entry(1, "  ", "iron.a.com")))[1])
    }

    // ── where a pairing goes ─────────────────────────────────────────
    private fun saved(slot: Int, host: String, token: Boolean = true) = Connections.Saved(slot, host, token)

    @Test fun theFirstPairingTakesSlotZero() {
        val t = Connections.pairTarget(emptyList(), "nori.example.com")
        assertEquals(0, t.slot); assertEquals(Connections.Kind.NEW, t.kind)
    }

    @Test fun aSecondServerGetsTheNextFreeSlot() {
        val t = Connections.pairTarget(listOf(saved(0, "nori.example.com")), "iron.example.com")
        assertEquals(1, t.slot); assertEquals(Connections.Kind.NEW, t.kind)
    }

    @Test fun aFreedSlotInTheMiddleIsReused() {
        val t = Connections.pairTarget(listOf(saved(0, "a.com"), saved(2, "c.com")), "b.com")
        assertEquals(1, t.slot)
    }

    @Test fun pairingTheSameServerAgainReplacesThatConnectionNotAddsOne() {
        val t = Connections.pairTarget(listOf(saved(0, "nori.example.com"), saved(1, "iron.example.com")), "IRON.example.com")
        assertEquals(1, t.slot); assertEquals(Connections.Kind.SAME_SERVER, t.kind)
    }

    @Test fun anUnpairedLeftoverIsReusedRatherThanPilingUp() {
        val t = Connections.pairTarget(listOf(saved(0, "old.example.com", token = false), saved(1, "iron.example.com")), "new.example.com")
        assertEquals(0, t.slot); assertEquals(Connections.Kind.REUSE_UNPAIRED, t.kind)
    }

    @Test fun theSameServerWinsOverAnUnpairedLeftover() {
        val t = Connections.pairTarget(listOf(saved(0, "old.example.com", token = false), saved(1, "iron.example.com")), "iron.example.com")
        assertEquals(1, t.slot); assertEquals(Connections.Kind.SAME_SERVER, t.kind)
    }

    @Test fun thereIsARoomLimit() {
        val full = (0 until Connections.MAX).map { saved(it, "h$it.example.com") }
        val t = Connections.pairTarget(full, "another.example.com")
        assertEquals(Connections.Kind.FULL, t.kind); assertEquals(Connections.NONE, t.slot)
    }

    // ── which connection a link or widget means ──────────────────────
    @Test fun aLinkThatNamesAnExistingConnectionGetsIt() {
        assertEquals(1, Connections.pick(1, listOf(0, 1), 0))
    }

    @Test fun anUnknownOrMissingOneFallsBackToTheTabInFrontThenToWhatExists() {
        assertEquals(0, Connections.pick(null, listOf(0, 1), 0))
        assertEquals(1, Connections.pick(3, listOf(0, 1), 1))
        assertEquals(2, Connections.pick(null, listOf(2), 0))      // the remembered tab was removed
        assertEquals(0, Connections.pick(null, emptyList(), 0))
    }

    // ── ids that two servers would share ─────────────────────────────
    @Test fun slotZeroNotificationIdsAreWhatTheyAlwaysWere() {
        assertEquals(100 + 902, Channels.notificationId(902))
        assertEquals(Channels.notificationId(902), Channels.notificationId(902, 0))
    }

    @Test fun theSameMessageNumberOnTwoServersIsTwoNotifications() {
        // both servers start counting at 1: without the connection in the id, the second would replace the first
        val ids = (0 until Connections.MAX).map { Channels.notificationId(7, it) }
        assertEquals(ids.size, ids.toSet().size)
    }

    @Test fun notificationRangesNeverOverlapAndStayPositive() {
        val extremes = listOf(0L, 1L, 299_999_999L, 300_000_000L, 999_999_999L, Long.MAX_VALUE, -5L)
        val seen = HashMap<Int, Pair<Int, Long>>()
        for (slot in 0 until Connections.MAX) for (m in extremes) {
            val id = Channels.notificationId(m, slot)
            assertTrue("id $id for slot $slot message $m is below the fixed ids", id >= 100)
            val earlier = seen.put(id, slot to m)
            // the same id may only repeat for the very same (slot, number mod its range): never across slots
            if (earlier != null) assertEquals("slots $slot and ${earlier.first} share notification id $id", earlier.first, slot)
        }
    }

    @Test fun placeFencesNameTheirConnection() {
        assertEquals("nori-place-5", Geo.requestId(5))                    // slot 0: exactly what earlier versions registered
        assertEquals("nori-place-5", Geo.requestId(5, 0))
        assertEquals("nori-place-s1-5", Geo.requestId(5, 1))
        assertNotEquals(Geo.requestId(5, 0), Geo.requestId(5, 1))
    }

    @Test fun fenceNamesRoundTripWithTheirSlot() {
        for (slot in 0 until Connections.MAX) assertEquals(slot to 42L, Geo.parseRequest(Geo.requestId(42, slot)))
        assertEquals(0 to 7L, Geo.parseRequest("nori-place-7"))           // a fence registered by an older version
        assertNull(Geo.parseRequest("someone-else-9"))
        assertNull(Geo.parseRequest("nori-place-sx-3"))
        assertNull(Geo.parseRequest("nori-place-s2-"))
    }

    @Test fun theOriginalFenceParserStillReadsSlotZeroNames() {
        assertEquals(42L, Geo.placeIdFromRequest(Geo.requestId(42)))
        assertNull(Geo.placeIdFromRequest(Geo.requestId(42, 1)))          // a connection above zero is not a slot-zero fence
    }

    // ── links ────────────────────────────────────────────────────────
    @Test fun aChatLinkNamesItsConnectionAndStillParsesAsTheChat() {
        for (slot in 0 until Connections.MAX) {
            val link = Deeplink.chatLink(slot)
            assertEquals(Link.Chat, Deeplink.parse(link))
            assertEquals(slot, Deeplink.connOf(link))
        }
    }

    @Test fun theOldChatLinkNamesNoConnection() {
        assertEquals(Link.Chat, Deeplink.parse("nori-app://chat"))
        assertNull(Deeplink.connOf("nori-app://chat"))
        assertNull(Deeplink.connOf(null))
    }

    @Test fun aConnectionOutsideTheRangeIsIgnored() {
        assertNull(Deeplink.connOf("nori-app://chat?conn=9"))
        assertNull(Deeplink.connOf("nori-app://chat?conn=-1"))
        assertNull(Deeplink.connOf("nori-app://chat?conn=abc"))
    }

    @Test fun aPrefillLinkCanNameAConnectionToo() {
        val link = Deeplink.prefillLink("Log water: ") + "&conn=1"
        assertEquals(Link.Prefill("Log water: "), Deeplink.parse(link))
        assertEquals(1, Deeplink.connOf(link))
    }
}
