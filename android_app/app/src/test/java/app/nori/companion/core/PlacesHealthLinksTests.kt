package app.nori.companion.core

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class GeoTest {
    @Test fun distanceMathIsRightWithinAMetre() {
        assertEquals(0.0, Geo.distanceM(43.0, -76.1, 43.0, -76.1), 0.001)
        // one degree of latitude is about 111.2 km
        assertEquals(111_195.0, Geo.distanceM(0.0, 0.0, 1.0, 0.0), 200.0)
        // New York to London is about 5570 km
        assertEquals(5_570_000.0, Geo.distanceM(40.7128, -74.0060, 51.5074, -0.1278), 20_000.0)
        // symmetric, and short distances work (0.001 degrees of latitude is about 111 m)
        assertEquals(Geo.distanceM(43.0, -76.1, 43.001, -76.1), Geo.distanceM(43.001, -76.1, 43.0, -76.1), 0.001)
        assertEquals(111.2, Geo.distanceM(43.0, -76.1, 43.001, -76.1), 1.0)
        // across the antimeridian
        assertEquals(22_240.0, Geo.distanceM(0.0, 179.9, 0.0, -179.9), 100.0)
    }

    @Test fun radiusIsClampedToWhatPhonesCanDeliver() {
        assertEquals(150, Geo.effectiveRadius(0))
        assertEquals(150, Geo.effectiveRadius(-20))
        assertEquals(100, Geo.effectiveRadius(30))
        assertEquals(120, Geo.effectiveRadius(120))
        assertEquals(5000, Geo.effectiveRadius(1_000_000))
    }

    @Test fun invalidCoordinatesAreRejected() {
        assertFalse(Geo.validLatLon(Double.NaN, 0.0)); assertFalse(Geo.validLatLon(91.0, 0.0)); assertFalse(Geo.validLatLon(0.0, 181.0)); assertFalse(Geo.validLatLon(0.0, 0.0))
        assertTrue(Geo.validLatLon(43.0, -76.1))
    }

    private fun place(id: Long, lat: Double = 43.0, lon: Double = -76.1, r: Int = 120, name: String = "P$id") = PlaceDef(id, name, lat, lon, r, false)

    @Test fun registrableCapsDedupesAndWidens() {
        val many = (1L..100L).map { place(it) }
        assertEquals(Geo.MAX_GEOFENCES, Geo.registrable(many).size)
        assertEquals(1, Geo.registrable(listOf(place(1), place(1))).size)
        assertEquals(100, Geo.registrable(listOf(place(1, r = 20))).first().radiusM)
        assertTrue(Geo.registrable(listOf(place(1, lat = 500.0))).isEmpty())
    }

    @Test fun signatureChangesOnlyWhenTheFencesDo() {
        val a = Geo.signature(listOf(place(1), place(2)))
        assertEquals(a, Geo.signature(listOf(place(2), place(1))))                         // order does not matter
        assertNotEquals(a, Geo.signature(listOf(place(1), place(2, lon = -76.2))))
        assertNotEquals(a, Geo.signature(listOf(place(1), place(2, name = "Renamed"))))     // the name is reported in snapshot.place
        assertNotEquals(a, Geo.signature(listOf(place(1))))
    }

    @Test fun nearestWithinFindsDuplicates() {
        val list = listOf(place(1, 43.0, -76.1), place(2, 43.5, -76.1))
        assertEquals(1L, Geo.nearestWithin(43.0002, -76.1, list, 60.0)!!.id)
        assertNull(Geo.nearestWithin(43.01, -76.1, list, 60.0))
    }

    @Test fun requestIdsRoundTrip() {
        assertEquals(42L, Geo.placeIdFromRequest(Geo.requestId(42)))
        assertNull(Geo.placeIdFromRequest("someone-else-9"))
    }
}

class PlaceTrackerTest {
    private val t0 = 1_800_000_000_000L

    @Test fun arrivalIsReportedOnceAndRepeatsAreDropped() {
        val t = PlaceTracker()
        assertEquals(PlaceEvent("arrived", 3, t0), t.onTransition("arrived", 3, t0))
        assertNull(t.onTransition("arrived", 3, t0 + 1000))
        assertEquals(3L, t.currentPlaceId())
    }

    @Test fun aLeaveIsHeldUntilItSettlesThenReportedWithItsOwnTime() {
        val t = PlaceTracker()
        t.onTransition("arrived", 3, t0)
        assertNull(t.onTransition("left", 3, t0 + 600_000))
        assertTrue(t.flush(t0 + 600_000 + PlaceTracker.SETTLE_MS - 1).isEmpty())
        val out = t.flush(t0 + 600_000 + PlaceTracker.SETTLE_MS)
        assertEquals(listOf(PlaceEvent("left", 3, t0 + 600_000)), out)
        assertNull(t.currentPlaceId())
        assertTrue(t.flush(t0 + 10_000_000).isEmpty())                                      // not reported twice
    }

    @Test fun flappingAtTheEdgeProducesNoLeftAndNoSecondArrival() {
        val t = PlaceTracker()
        t.onTransition("arrived", 3, t0)
        assertNull(t.onTransition("left", 3, t0 + 300_000))
        assertNull(t.onTransition("arrived", 3, t0 + 360_000))                              // back inside within the settle time
        assertTrue(t.flush(t0 + 10_000_000).isEmpty())
        assertEquals(3L, t.currentPlaceId())
    }

    @Test fun repeatedLeavesAreOneEvent() {
        val t = PlaceTracker()
        t.onTransition("arrived", 3, t0)
        t.onTransition("left", 3, t0 + 1000)
        t.onTransition("left", 3, t0 + 2000)
        assertEquals(1, t.flush(t0 + 1_000_000).size)
    }

    @Test fun aLeaveWithoutAnEarlierArrivalIsStillReported() {
        val t = PlaceTracker()
        t.onTransition("left", 7, t0)
        assertEquals(listOf(PlaceEvent("left", 7, t0)), t.flush(t0 + PlaceTracker.SETTLE_MS))
    }

    @Test fun twoPlacesAreIndependentAndCurrentIsTheLatestArrival() {
        val t = PlaceTracker()
        t.onTransition("arrived", 1, t0)
        t.onTransition("arrived", 2, t0 + 5000)
        assertEquals(2L, t.currentPlaceId())
        t.onTransition("left", 2, t0 + 9000)
        assertEquals(1L, t.currentPlaceId())                                                // a held leave no longer counts as "here"
    }

    @Test fun unknownKindsAreIgnored() {
        assertNull(PlaceTracker().onTransition("teleported", 1, t0))
    }

    @Test fun forgetsPlacesThatNoLongerExist() {
        val t = PlaceTracker()
        t.onTransition("arrived", 1, t0); t.onTransition("arrived", 2, t0 + 1)
        t.retain(setOf(2L))
        assertEquals(2L, t.currentPlaceId()); assertFalse(t.isInside(1))
    }

    @Test fun stateSurvivesARestart() {
        val t = PlaceTracker()
        t.onTransition("arrived", 1, t0); t.onTransition("arrived", 2, t0 + 1); t.onTransition("left", 1, t0 + 2)
        val again = PlaceTracker.parse(t.serialize())
        assertEquals(2L, again.currentPlaceId()); assertTrue(again.hasPending())
        assertEquals(listOf(PlaceEvent("left", 1, t0 + 2)), again.flush(t0 + 1_000_000))
        assertNull(PlaceTracker.parse("garbage").currentPlaceId())
    }

    @Test fun eventsNeverCarryCoordinates() {
        val json = PlaceEvent("arrived", 3, t0).toJson()
        assertEquals(setOf("kind", "place_id", "ts"), json.keys().asSequence().toSet())
    }
}

class EventOutboxTest {
    @Test fun dedupesBoundsAndBatchesAtFifty() {
        val o = EventOutbox()
        o.add(PlaceEvent("arrived", 1, 100)); o.add(PlaceEvent("arrived", 1, 100))
        assertEquals(1, o.size())
        for (i in 1L..300L) o.add(PlaceEvent("left", 2, 1000 + i))
        assertEquals(EventOutbox.MAX_QUEUED, o.size())
        assertEquals(50, o.peek().size)
        assertEquals(50, o.body().getJSONArray("events").length())
        o.drop(50)
        assertEquals(EventOutbox.MAX_QUEUED - 50, o.size())
    }

    @Test fun survivesARestartAndIgnoresGarbage() {
        val o = EventOutbox()
        o.add(PlaceEvent("arrived", 1, 5)); o.add(PlaceEvent("left", 1, 9))
        val again = EventOutbox.parse(o.serialize())
        assertEquals(listOf(PlaceEvent("arrived", 1, 5), PlaceEvent("left", 1, 9)), again.peek())
        assertEquals(0, EventOutbox.parse("nope").size())
        assertEquals(0, EventOutbox.parse("""[{"kind":"exploded","place_id":1,"ts":1}]""").size())
    }

    @Test fun bodyHasTheContractShape() {
        val o = EventOutbox(listOf(PlaceEvent("arrived", 3, 1792000000000L)))
        val e = o.body().getJSONArray("events").getJSONObject(0)
        assertEquals("arrived", e.getString("kind")); assertEquals(3L, e.getLong("place_id")); assertEquals(1792000000000L, e.getLong("ts"))
    }
}

class HealthTest {
    private val now = 1_800_000_000_000L
    private fun weight(id: String, ts: Long, v: Double = 172.4) = HealthSample("weight", value = v, unit = "lb", ts = ts, id = id)
    private fun bp(id: String, ts: Long) = HealthSample("bp", systolic = 120, diastolic = 80, ts = ts, id = id)
    private fun steps(day: String, n: Double) = HealthSample("steps", value = n, day = day)

    @Test fun consentNeedsServerAndOwnerAndEachMetric() {
        val all = HealthMetrics.ALL
        assertEquals(setOf("weight", "steps"), HealthMetrics.enabled(true, all, true, listOf("weight", "steps")))
        assertEquals(setOf("weight"), HealthMetrics.enabled(true, listOf("weight", "bp"), true, listOf("weight", "steps")))
        assertTrue(HealthMetrics.enabled(false, all, true, all).isEmpty())          // the server says no
        assertTrue(HealthMetrics.enabled(true, all, false, all).isEmpty())          // the owner says no
        assertTrue(HealthMetrics.enabled(true, all, true, emptyList()).isEmpty())   // no metric switched on
        assertTrue(HealthMetrics.enabled(true, emptyList(), true, all).isEmpty())
        assertEquals(setOf("bp"), HealthMetrics.enabled(true, listOf("BP", "unknown"), true, listOf("Bp")))
    }

    @Test fun nothingIsSentTwice() {
        val l = HealthLedger()
        val first = l.filterNew(listOf(weight("hc-1", 100), bp("hc-2", 101)))
        assertEquals(2, first.size)
        l.markSent(first)
        assertTrue(l.filterNew(listOf(weight("hc-1", 100), bp("hc-2", 101))).isEmpty())
        assertEquals(listOf("hc-3"), l.filterNew(listOf(weight("hc-1", 100), weight("hc-3", 200))).map { it.id })
    }

    @Test fun anUnsentReadingIsOfferedAgainBecauseOnlyMarkSentRemembers() {
        val l = HealthLedger()
        assertEquals(1, l.filterNew(listOf(weight("hc-1", 100))).size)
        assertEquals(1, l.filterNew(listOf(weight("hc-1", 100))).size)               // the POST failed, nothing was marked
    }

    @Test fun duplicatesInsideOneBatchCollapse() {
        assertEquals(1, HealthLedger().filterNew(listOf(weight("hc-1", 100), weight("hc-1", 100))).size)
    }

    @Test fun readingsWithoutAnIdAreRecognisedByTimestamp() {
        val l = HealthLedger()
        val s = HealthSample("weight", value = 170.0, unit = "lb", ts = 555, id = null)
        l.markSent(l.filterNew(listOf(s)))
        assertTrue(l.filterNew(listOf(s)).isEmpty())
        assertEquals(1, l.filterNew(listOf(s.copy(ts = 556))).size)
    }

    @Test fun dailyTotalsAreResentOnlyWhenTheNumberChanges() {
        val l = HealthLedger()
        l.markSent(l.filterNew(listOf(steps("2026-10-21", 3000.0))))
        assertTrue(l.filterNew(listOf(steps("2026-10-21", 3000.0))).isEmpty())
        val grown = l.filterNew(listOf(steps("2026-10-21", 5400.0)))
        assertEquals(1, grown.size)
        l.markSent(grown)
        assertEquals(1, l.filterNew(listOf(steps("2026-10-22", 5400.0))).size)         // a new day is new
        assertTrue(l.filterNew(listOf(steps("2026-10-21", 5400.0))).isEmpty())
    }

    @Test fun sleepAndRestingHrAreDailyToo() {
        val l = HealthLedger()
        val sleep = HealthSample("sleep", minutes = 432, day = "2026-10-21")
        val hr = HealthSample("resting_hr", value = 58.0, day = "2026-10-21")
        l.markSent(l.filterNew(listOf(sleep, hr)))
        assertTrue(l.filterNew(listOf(sleep, hr)).isEmpty())
        assertEquals(1, l.filterNew(listOf(sleep.copy(minutes = 440), hr)).size)
    }

    @Test fun unknownMetricTypesAreDropped() {
        assertTrue(HealthLedger().filterNew(listOf(HealthSample("blood_sugar", value = 5.0, day = "2026-10-21"))).isEmpty())
    }

    @Test fun watermarkStartsFourteenDaysBackThenOverlapsTheLastReadingByADay() {
        val l = HealthLedger()
        assertEquals(now - 14 * HealthLedger.DAY_MS, l.sinceMs("weight", now))
        l.markSent(listOf(weight("hc-1", now - 2 * HealthLedger.DAY_MS)))
        assertEquals(now - 3 * HealthLedger.DAY_MS, l.sinceMs("weight", now))
        assertEquals(now - 14 * HealthLedger.DAY_MS, l.sinceMs("bp", now))               // per metric
        l.markSent(listOf(weight("hc-0", now - 40 * HealthLedger.DAY_MS)))              // an ancient reading never pushes the window past 14 days
        assertEquals(now - 3 * HealthLedger.DAY_MS, l.sinceMs("weight", now))
    }

    @Test fun ledgerSurvivesARestartAndIsBounded() {
        val l = HealthLedger()
        l.markSent((1..700).map { weight("hc-$it", it.toLong()) })
        val again = HealthLedger.parse(l.serialize())
        assertTrue(again.filterNew(listOf(weight("hc-700", 700))).isEmpty())
        assertEquals(1, again.filterNew(listOf(weight("hc-1", 1))).size)                  // the oldest were forgotten (bounded memory)
        assertTrue(HealthLedger.parse("garbage").filterNew(listOf(weight("a", 1))).isNotEmpty())
    }

    @Test fun wireFormatMatchesTheContract() {
        val w = weight("hc-123", 1792000000000L, 172.4).toJson()
        assertEquals("weight", w.getString("type")); assertEquals(172.4, w.getDouble("value"), 0.0001); assertEquals("lb", w.getString("unit")); assertEquals("hc-123", w.getString("id")); assertEquals(1792000000000L, w.getLong("ts"))
        val b = bp("hc-124", 1792000000000L).toJson()
        assertEquals("bp", b.getString("type")); assertEquals(120, b.getInt("systolic")); assertEquals(80, b.getInt("diastolic"))
        val s = steps("2026-10-21", 5400.0).toJson()
        assertEquals(5400, s.getInt("value")); assertEquals("2026-10-21", s.getString("day")); assertFalse(s.has("ts"))
        val sl = HealthSample("sleep", minutes = 432, day = "2026-10-21").toJson()
        assertEquals(432, sl.getInt("minutes")); assertEquals("2026-10-21", sl.getString("day"))
        val r = HealthSample("resting_hr", value = 58.0, day = "2026-10-21").toJson()
        assertEquals(58, r.getInt("value"))
        assertEquals(2, HealthLedger.body(listOf(weight("a", 1), bp("b", 2))).getJSONArray("samples").length())
    }

    @Test fun kilogramsBecomePoundsRoundedToOneDecimal() {
        assertEquals(172.4, HealthMetrics.kgToLb(78.2), 0.0001)
        assertEquals(0.0, HealthMetrics.kgToLb(0.0), 0.0)
    }
}

class LinksTest {
    @Test fun serverAddressesMustBeHttps() {
        assertEquals("https://nori.example.com", ServerUrl.normalize("https://nori.example.com/"))
        assertEquals("https://nori.example.com", ServerUrl.normalize("nori.example.com"))
        assertEquals("https://nori.example.com:8443", ServerUrl.normalize("  HTTPS://Nori.Example.com:8443/?x=1#y  "))
        assertEquals("https://example.com/nori", ServerUrl.normalize("https://example.com/nori/"))
        for (bad in listOf("http://nori.example.com", "ftp://x", "javascript:alert(1)", "https://user:pw@nori.example.com", "https://", "", "https://exa mple.com", "https://x.com:99999", "https://x.com\\evil"))
            assertNull("$bad must be refused", ServerUrl.normalize(bad))
    }

    @Test fun hostAndPageHelpers() {
        assertEquals("nori.example.com", ServerUrl.host("https://nori.example.com:8443/x"))
        assertEquals("https://nori.example.com/?talk=1", ServerUrl.page("https://nori.example.com", "/?talk=1"))
        assertEquals("https://nori.example.com/a", ServerUrl.page("https://nori.example.com/", "a"))
    }

    @Test fun pairingCodesAreNormalised() {
        assertEquals("ABCD-EFGH", PairCode.normalize("abcd-efgh"))
        assertEquals("ABCD-EFGH", PairCode.normalize(" abcd efgh "))
        assertEquals("ABCD-2345", PairCode.normalize("ABCD2345"))
        for (bad in listOf("", "ABC-DEFG", "ABCD-EFGHI", "----", null)) assertNull(PairCode.normalize(bad))
    }

    @Test fun pairingDeepLinkIsParsed() {
        val l = Deeplink.parse("nori-pair://pair?code=abcd-efgh&server=https%3A%2F%2Fnori.example.com") as Link.Pair
        assertEquals("ABCD-EFGH", l.code); assertEquals("https://nori.example.com", l.server)
        val raw = Deeplink.parse("nori-pair://pair?code=ABCD-EFGH&server=https://nori.example.com") as Link.Pair
        assertEquals("https://nori.example.com", raw.server)
        assertNull((Deeplink.parse("nori-pair://pair?code=ABCD-EFGH") as Link.Pair).server)
    }

    @Test fun hostilePairingLinksAreRejectedOrNeutralised() {
        assertEquals(Link.Unknown, Deeplink.parse("nori-pair://pair?server=https://evil.example"))                       // no code
        assertEquals(Link.Unknown, Deeplink.parse("nori-pair://pair?code=short"))
        assertNull((Deeplink.parse("nori-pair://pair?code=ABCD-EFGH&server=http://evil.example") as Link.Pair).server)      // cleartext server dropped, never used
        assertNull((Deeplink.parse("nori-pair://pair?code=ABCD-EFGH&server=javascript:alert(1)") as Link.Pair).server)
        assertEquals(Link.Unknown, Deeplink.parse("https://evil.example/nori-pair://pair?code=ABCD-EFGH"))
        assertEquals(Link.Unknown, Deeplink.parse(null))
    }

    @Test fun appLinksAreRoutedByFixedHostsOnly() {
        assertEquals(Link.Settings, Deeplink.parse("nori-app://settings"))
        assertEquals(Link.Talk, Deeplink.parse("nori-app://talk"))
        assertEquals(Link.Chat, Deeplink.parse("nori-app://chat"))
        assertEquals(Link.AddTask, Deeplink.parse("nori-app://task"))
        assertEquals(Link.Unknown, Deeplink.parse("nori-app://open?url=https://evil.example"))
        assertEquals(Link.Unknown, Deeplink.parse("nori-app://"))
        assertEquals(Link.Unknown, Deeplink.parse("other-app://settings"))
    }

    @Test fun prefillRoundTripsAndNeverBecomesAUrl() {
        val link = Deeplink.prefillLink("Log water: ")
        assertEquals(Link.Prefill("Log water: "), Deeplink.parse(link))
        assertEquals("/?prefill=Log%20water%3A%20", Deeplink.prefillPath("Log water: "))
        assertEquals("/?prefill=a%26b%3Dc%23d%2Fe", Deeplink.prefillPath("a&b=c#d/e"))        // cannot add parameters or leave the page
        assertEquals("/?prefill=%F0%9F%92%A7", Deeplink.prefillPath("💧"))
        assertEquals(Link.Chat, Deeplink.parse("nori-app://prefill?text="))
        assertTrue(Deeplink.prefillPath("x".repeat(1000)).length < 500)
        assertEquals("/?talk=1", Deeplink.TALK_PATH)
    }

    @Test fun shareTextBecomesALinkOnlyWhenItIsEssentiallyAnAddress() {
        val link = ShareBody.fromText("https://example.com/a?b=1", "A page", "chat")!!
        assertEquals("link", link.getString("kind")); assertEquals("https://example.com/a?b=1", link.getString("url")); assertEquals("A page", link.getString("title")); assertEquals("chat", link.getString("dest"))
        val text = ShareBody.fromText("Remember to see https://example.com " + "words ".repeat(60), null, "note")!!
        assertEquals("text", text.getString("kind")); assertEquals("note", text.getString("dest"))
    }

    @Test fun shareBodiesAreBoundedAndValidated() {
        assertEquals(ShareBody.MAX_TEXT, ShareBody.fromText("x".repeat(20_000), null, "chat")!!.getString("text").length)
        assertNull(ShareBody.fromText("   ", null, "chat"))
        assertNull(ShareBody.fromText(null, null, "chat"))
        assertNull(ShareBody.fromText("hi", null, "inbox"))
        assertNull(ShareBody.fromText("hi", null, ""))
        assertEquals("task", ShareBody.fromText("buy milk", null, "task")!!.getString("dest"))
        assertEquals("check the date", ShareBody.fromText("hi", null, "note", " check the date ")!!.getString("note"))
        assertFalse(ShareBody.fromText("hi", null, "note", "   ")!!.has("note"))
        val img = ShareBody.image("QUJD", "caption", "chat")!!
        assertEquals("image", img.getString("kind")); assertEquals("QUJD", img.getString("jpeg_b64")); assertEquals("image/jpeg", img.getString("mime"))
        assertNull(ShareBody.image("", null, "chat")); assertNull(ShareBody.image("QUJD", null, "elsewhere"))
    }

    @Test fun versionComparison() {
        assertTrue(UpdateLogic.isNewer("0.2.0", "0.1.9")); assertTrue(UpdateLogic.isNewer("1.0", "0.9.9")); assertTrue(UpdateLogic.isNewer("0.10.0", "0.9.0"))
        assertFalse(UpdateLogic.isNewer("0.1.0", "0.1.0")); assertFalse(UpdateLogic.isNewer("0.1.0", "0.2.0")); assertFalse(UpdateLogic.isNewer("", "0.1.0"))
        assertFalse(UpdateLogic.isNewer("0.1", "0.1.0"))
    }

    @Test fun anUpdateIsAnnouncedOncePerVersion() {
        assertTrue(UpdateLogic.shouldNotify("0.2.0", "0.1.0", ""))
        assertFalse(UpdateLogic.shouldNotify("0.2.0", "0.1.0", "0.2.0"))
        assertTrue(UpdateLogic.shouldNotify("0.3.0", "0.1.0", "0.2.0"))
        assertFalse(UpdateLogic.shouldNotify("0.1.0", "0.1.0", ""))
        assertFalse(UpdateLogic.shouldNotify("", "0.1.0", ""))
    }

    @Test fun theTokenIsOnlyEverSentToThePairedServer() {
        val base = "https://nori.example.com"
        assertEquals("https://nori.example.com/device/app.apk", UpdateLogic.resolveApkUrl(base, "/device/app.apk"))
        assertEquals("https://nori.example.com/device/app.apk", UpdateLogic.resolveApkUrl("$base/", "/device/app.apk"))
        assertEquals("https://nori.example.com/device/app.apk", UpdateLogic.resolveApkUrl(base, "https://nori.example.com/device/app.apk"))
        for (bad in listOf("https://evil.example/device/app.apk", "//evil.example/app.apk", "http://nori.example.com/app.apk", "https://nori.example.com@evil.example/app.apk",
            "https://nori.example.com.evil.example/app.apk", "javascript:alert(1)", "file:///sdcard/x.apk", "app.apk", "", "/a b", "/x\\y"))
            assertNull("$bad must be refused", UpdateLogic.resolveApkUrl(base, bad))
        assertNull(UpdateLogic.resolveApkUrl(base, null))
    }
}

class ChannelsAndLogTest {
    @Test fun categoryDrivesTheChannel() {
        assertEquals(Channels.CHAT, Channels.forCategory("chat"))
        assertEquals(Channels.CHAT, Channels.forCategory("ping"))
        assertEquals(Channels.REMINDER, Channels.forCategory("reminder"))
        assertEquals(Channels.APPROVAL, Channels.forCategory("approval"))
        assertEquals(Channels.JOB, Channels.forCategory("job"))
        assertEquals(Channels.PEER, Channels.forCategory("peer"))
        assertEquals(Channels.CHAT, Channels.forCategory("CHAT"))
        assertEquals(Channels.JOB, Channels.forCategory("something-new"))            // an unknown category never gets an interrupting channel
        assertEquals(Channels.JOB, Channels.forCategory(""))
    }

    @Test fun importanceMatchesTheDesign() {
        assertEquals(Channels.IMPORTANCE_HIGH, Channels.importanceOf(Channels.CHAT))
        assertEquals(Channels.IMPORTANCE_HIGH, Channels.importanceOf(Channels.REMINDER))
        assertEquals(Channels.IMPORTANCE_HIGH, Channels.importanceOf(Channels.APPROVAL))
        assertEquals(Channels.IMPORTANCE_DEFAULT, Channels.importanceOf(Channels.JOB))
        assertEquals(Channels.IMPORTANCE_LOW, Channels.importanceOf(Channels.PEER))
        assertEquals(Channels.IMPORTANCE_LOW, Channels.importanceOf(Channels.SYNC))
        assertEquals(Channels.IMPORTANCE_LOW, Channels.importanceOf(Channels.WINDOWS))
        assertNotNull(Channels.ALL.firstOrNull { it.id == Channels.UPDATES })
        assertEquals(Channels.ALL.size, Channels.ALL.map { it.id }.toSet().size)
    }

    @Test fun notificationIdsAreStableAndClearOfTheFixedOnes() {
        assertEquals(Channels.notificationId(902), Channels.notificationId(902))
        assertTrue(Channels.notificationId(0) >= 100)
        assertTrue(Channels.notificationId(Long.MAX_VALUE) >= 100)
        assertNotEquals(Channels.notificationId(1), Channels.notificationId(2))
    }

    @Test fun titleFallsBackToAGenericNameNotNori() {      // a server that sends no title: the app does not know the assistant's name here, and must not guess "Nori"
        assertEquals("Assistant", Channels.titleFor(Message(1, "chat", "", "t", 0, null)))
        assertEquals("Hazel", Channels.titleFor(Message(1, "chat", "Hazel", "t", 0, null)))
    }

    @Test fun sentLogKeepsTheLastHundredTwentyNewestFirst() {
        val log = RingLog()
        for (i in 1..200) log.add("line $i")
        assertEquals(120, log.all().size)
        assertEquals("line 200", log.recent(1)[0])
        assertEquals("line 81", log.all().first())
        log.add("two\nlines")
        assertFalse(log.all().last().contains("\n"))
        val again = RingLog.parse(log.serialize())
        assertEquals(log.all(), again.all())
        log.clear(); assertTrue(log.all().isEmpty())
    }

    @Test fun statusLineTellsTheTruth() {
        val now = 1_800_000_000_000L
        assertTrue(StatusLine.build(false, false, false, now, 0, 0, "", "").startsWith("Not paired"))
        assertTrue(StatusLine.build(true, true, false, now, 0, 0, "", "x").contains("Pair it again"))
        assertTrue(StatusLine.build(true, false, true, now, now + 60_000, 0, "", "x").startsWith("PAUSED"))
        assertFalse(StatusLine.build(true, false, true, now, now + 60_000, 0, "", "x").contains("may act"))      // paused hides the windows
        val open = StatusLine.build(true, false, false, now, now + 90_000, now + 30_000, "Connected", "nori.example.com")
        assertTrue(open.contains("Connected to nori.example.com")); assertTrue(open.contains("1:30")); assertTrue(open.contains("0:30"))
        assertTrue(StatusLine.build(true, false, false, now, now - 1, 0, "Can't reach Nori", "x").contains("Can't reach Nori"))
        assertFalse(StatusLine.build(true, false, false, now, now - 1, 0, "Connected", "x").contains("may act"))
        assertNotNull(JSONObject())
    }
}
