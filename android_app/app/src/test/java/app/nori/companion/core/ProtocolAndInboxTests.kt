package app.nori.companion.core

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ProtocolTest {
    private val samplePoll = """
    {"active":true,
     "messages":[
       {"id":903,"category":"approval","title":"Nori","text":"Needs a yes","ts":1792000000500,"approval":{"id":12,"summary":"click button \"Send\"","host":"x.com"}},
       {"id":902,"category":"reminder","title":"Nori","text":"Take the bins out","ts":1792000000000,"approval":null}],
     "command":{"id":77,"action":"open_url","args":{"url":"https://example.com"},"approved":false,"force_approval":true},
     "settings":{"notifications":true,"health":true,"places":false,"actions":true,"control":false,"screen":false,"quiet":true,
        "hidden_apps":["com.bank.app"],"never_control_apps":["com.game"],"health_metrics":["weight","BP","steps"],
        "places_list":[{"id":3,"name":"Home","lat":43.0,"lon":-76.1,"radius_m":120,"home":true},{"id":4,"name":"Bad","lat":999,"lon":0}],
        "actions_per_hour":12},
     "app":{"latest":"0.2.0","apk_url":"/device/app.apk"}}"""

    @Test fun pollRequestHasEveryContractKey() {
        val body = Protocol.pollRequest("0.1.0", false, true, 901, Caps(true, true, false, false, true, false, false), 0, 1792000000000L,
            Snapshot(87, false, "wifi", true, false, false, "Home"))
        for (k in listOf("v", "paused", "app_visible", "ack", "caps", "interact_until", "share_until", "snapshot")) assertTrue("missing $k", body.has(k))
        assertEquals(901L, body.getLong("ack"))
        assertTrue(body.getBoolean("app_visible"))
        assertEquals(1792000000000L, body.getLong("share_until"))
        val snap = body.getJSONObject("snapshot")
        assertEquals(87, snap.getInt("battery")); assertEquals("wifi", snap.getString("network")); assertEquals("Home", snap.getString("place"))
        for (k in listOf("charging", "screen_on", "locked", "dnd")) assertTrue(snap.has(k))
        val caps = body.getJSONObject("caps")
        for (k in listOf("notifications", "share", "health", "places", "actions", "screen", "control")) assertTrue(caps.has(k))
    }

    @Test fun snapshotNeverCarriesCoordinatesOnlyAPlaceName() {
        val s = Snapshot(50, true, "cellular", false, true, true, "Work").toJson().toString()
        assertFalse(s.contains("lat")); assertFalse(s.contains("lon"))
        assertFalse(Snapshot(50, true, "wifi", true, false, false, null).toJson().has("place"))
        assertFalse(Snapshot(50, true, "wifi", true, false, false, "  ").toJson().has("place"))
        assertFalse(Snapshot(null, true, "wifi", true, false, false, null).toJson().has("battery"))
        assertFalse(Snapshot(500, true, "wifi", true, false, false, null).toJson().has("battery"))
    }

    @Test fun pausedHeartbeatSaysNothingElse() {
        val b = Protocol.pausedHeartbeat("0.1.0", 55)
        assertTrue(b.getBoolean("paused")); assertEquals(55L, b.getLong("ack")); assertFalse(b.has("snapshot"))
        assertEquals(0L, b.getLong("interact_until")); assertEquals(0L, b.getLong("share_until"))
        val caps = b.getJSONObject("caps")
        for (k in caps.keys()) assertFalse("$k must be off while paused", caps.getBoolean(k))
    }

    @Test fun parsesMessagesCommandSettingsAndAppInfo() {
        val r = Protocol.parsePoll(samplePoll)!!
        assertTrue(r.active)
        assertEquals(listOf(902L, 903L), r.messages.map { it.id })                       // oldest first even if the server did not order them
        assertEquals("reminder", r.messages[0].category)
        assertNull(r.messages[0].approval)
        assertEquals(12L, r.messages[1].approval!!.id)
        assertEquals("x.com", r.messages[1].approval!!.host)
        val c = r.command!!
        assertEquals(77L, c.id); assertEquals("open_url", c.action); assertEquals("https://example.com", c.args.getString("url")); assertFalse(c.approved); assertTrue(c.forceApproval)
        val s = r.settings!!
        assertTrue(s.notifications); assertTrue(s.health); assertFalse(s.places); assertTrue(s.actions); assertFalse(s.control); assertTrue(s.quiet)
        assertEquals(listOf("com.bank.app"), s.hiddenApps); assertEquals(listOf("com.game"), s.neverControlApps)
        assertEquals(listOf("weight", "bp", "steps"), s.healthMetrics)                    // lower-cased
        assertEquals(1, s.placesList.size)                                                // the place with impossible coordinates is dropped
        assertEquals("Home", s.placesList[0].name); assertEquals(120, s.placesList[0].radiusM); assertTrue(s.placesList[0].home)
        assertEquals(12, s.actionsPerHour)
        assertEquals("0.2.0", r.app!!.latest); assertEquals("/device/app.apk", r.app!!.apkUrl)
    }

    @Test fun nullCommandAndMissingSectionsAreFine() {
        val r = Protocol.parsePoll("""{"active":true,"messages":[],"command":null}""")!!
        assertNull(r.command); assertNull(r.settings); assertNull(r.app); assertTrue(r.messages.isEmpty())
    }

    @Test fun missingSettingsKeysDefaultToOffExceptNotifications() {
        val s = DeviceSettings.fromJson(JSONObject("""{}"""))
        assertTrue(s.notifications)
        assertFalse(s.health); assertFalse(s.places); assertFalse(s.actions); assertFalse(s.control); assertFalse(s.screen); assertFalse(s.quiet)
        assertTrue(s.healthMetrics.isEmpty()); assertTrue(s.hiddenApps.isEmpty())
        val none = DeviceSettings.parse("not json")
        assertFalse(none.actions)
    }

    @Test fun settingsSurviveARoundTripThroughText() {
        val s = Protocol.parsePoll(samplePoll)!!.settings!!
        assertEquals(s, DeviceSettings.parse(s.toJson().toString()))
    }

    @Test fun hostileOrBrokenPollsDoNotCrash() {
        assertNull(Protocol.parsePoll("not json"))
        assertNull(Protocol.parsePoll(null))
        assertNull(Protocol.parsePoll(""))
        val r = Protocol.parsePoll("""{"messages":[{"no":"id"},{"id":-1},{"id":5,"category":null}],"command":{"action":"x"}}""")!!
        assertEquals(listOf(5L), r.messages.map { it.id })
        assertNull(r.command)                                                              // a command without an id is ignored
    }

    @Test fun atMostTwentyMessagesPerPoll() {
        val msgs = (1..30).joinToString(",") { """{"id":$it,"category":"chat","text":"m$it"}""" }
        val r = Protocol.parsePoll("""{"messages":[$msgs]}""")!!
        assertEquals(20, r.messages.size)
        assertEquals(1L, r.messages.first().id)
    }

    @Test fun resultBodiesFollowTheContract() {
        val ok = Protocol.resultOk(77, JSONObject().put("ok", true))
        assertEquals(77L, ok.getLong("id")); assertTrue(ok.getBoolean("ok")); assertTrue(ok.has("result"))
        val err = Protocol.resultError(77, "no good")
        assertFalse(err.getBoolean("ok")); assertEquals("no good", err.getString("error"))
        assertTrue(Protocol.resultError(1, "").getString("error").isNotBlank())               // never blank
        assertTrue(Protocol.resultError(1, "   ").getString("error").isNotBlank())
        val na = Protocol.resultNeedsApproval(78, "this sends a message", "button \"Send\"", "f1a2b3c4")
        assertTrue(na.getBoolean("needs_approval")); assertFalse(na.has("ok")); assertEquals("this sends a message", na.getString("reason"))
        assertEquals("button \"Send\"", na.getJSONObject("element").getString("describe")); assertEquals("f1a2b3c4", na.getJSONObject("element").getString("fingerprint"))
    }

    @Test fun approvalAckRegisterAndPlaceBodies() {
        assertEquals("approve", Protocol.approval(12, true).getString("decision"))
        assertEquals("deny", Protocol.approval(12, false).getString("decision"))
        assertEquals(12L, Protocol.approval(12, true).getLong("id"))
        assertEquals(9L, Protocol.ack(9).getLong("id"))
        val reg = Protocol.register("ABCD-EFGH", "Pixel", "Pixel 8", "15", "0.1.0")
        assertEquals("phone", reg.getString("kind")); assertEquals("ABCD-EFGH", reg.getString("code"))
        for (k in listOf("name", "model", "android", "v")) assertTrue(reg.has(k))
        val p = Protocol.place("Home", 43.0, -76.1, null, true)
        assertEquals("Home", p.getString("name")); assertTrue(p.getBoolean("home")); assertFalse(p.has("radius_m"))
        assertEquals(150, Protocol.place("Gym", 1.0, 2.0, 150, false).getInt("radius_m"))
    }
}

class MessageInboxTest {
    private fun msg(id: Long, cat: String = "chat") = Message(id, cat, "Nori", "text $id", 0, null)

    @Test fun showsEachMessageOnceAndAcksTheHighestShown() {
        val inbox = MessageInbox()
        val shown = ArrayList<Long>()
        val n = inbox.process(listOf(msg(1), msg(2), msg(3))) { shown.add(it.id); ShowOutcome.SHOWN }
        assertEquals(3, n); assertEquals(listOf(1L, 2L, 3L), shown); assertEquals(3L, inbox.ackValue())
    }

    @Test fun duplicateIdsAreNotShownTwice() {
        val inbox = MessageInbox()
        val shown = ArrayList<Long>()
        inbox.process(listOf(msg(5), msg(5), msg(5))) { shown.add(it.id); ShowOutcome.SHOWN }
        assertEquals(listOf(5L), shown)
        inbox.process(listOf(msg(5))) { shown.add(it.id); ShowOutcome.SHOWN }                  // a redelivery after a lost response
        assertEquals(listOf(5L), shown)
        assertEquals(5L, inbox.ackValue())
    }

    @Test fun outOfOrderMessagesAreHandledOldestFirst() {
        val inbox = MessageInbox()
        val shown = ArrayList<Long>()
        inbox.process(listOf(msg(9), msg(7), msg(8))) { shown.add(it.id); ShowOutcome.SHOWN }
        assertEquals(listOf(7L, 8L, 9L), shown); assertEquals(9L, inbox.ackValue())
    }

    @Test fun aFailureStopsTheWalkSoTheAckNeverJumpsOverAnUnshownMessage() {
        val inbox = MessageInbox(ackedUpTo = 10)
        val n = inbox.process(listOf(msg(11), msg(12), msg(13))) { if (it.id == 12L) ShowOutcome.FAILED else ShowOutcome.SHOWN }
        assertEquals(1, n)
        assertEquals(11L, inbox.ackValue())                  // 12 and 13 are not acknowledged, so the server sends them again
        assertFalse(inbox.hasSeen(12)); assertFalse(inbox.hasSeen(13))
        val again = ArrayList<Long>()
        inbox.process(listOf(msg(11), msg(12), msg(13))) { again.add(it.id); ShowOutcome.SHOWN }
        assertEquals(listOf(12L, 13L), again)                // 11 was already shown and is not repeated
        assertEquals(13L, inbox.ackValue())
    }

    @Test fun nothingIsAckedWhenTheFirstMessageFails() {
        val inbox = MessageInbox(ackedUpTo = 40)
        inbox.process(listOf(msg(41), msg(42))) { ShowOutcome.FAILED }
        assertEquals(40L, inbox.ackValue())
    }

    @Test fun skippedMessagesAreHandledButNotShown() {
        val inbox = MessageInbox()
        val shown = ArrayList<Long>()
        val n = inbox.process(listOf(msg(1, "chat"), msg(2, "reminder"))) { if (it.category == "chat") ShowOutcome.SKIPPED else { shown.add(it.id); ShowOutcome.SHOWN } }
        assertEquals(1, n); assertEquals(listOf(2L), shown); assertEquals(2L, inbox.ackValue())
    }

    @Test fun theAckNeverGoesBackwards() {
        val inbox = MessageInbox(ackedUpTo = 100)
        inbox.process(listOf(msg(50))) { ShowOutcome.SHOWN }          // an old id after the server restarted its counter
        assertEquals(100L, inbox.ackValue())
        val shown = ArrayList<Long>()
        inbox.process(listOf(msg(51))) { shown.add(it.id); ShowOutcome.SHOWN }
        assertEquals(listOf(51L), shown)                              // still shown: it was never seen, even though it is below the ack
    }

    @Test fun seenIdsSurviveARestartAndAreBounded() {
        val a = MessageInbox()
        a.process((1L..5L).map { msg(it) }) { ShowOutcome.SHOWN }
        val b = MessageInbox(a.ackValue(), MessageInbox.parseSeen(a.serializeSeen()))
        val shown = ArrayList<Long>()
        b.process(listOf(msg(3), msg(6))) { shown.add(it.id); ShowOutcome.SHOWN }
        assertEquals(listOf(6L), shown)
        val big = MessageInbox()
        big.process((1L..400L).map { msg(it) }) { ShowOutcome.SHOWN }
        assertEquals(MessageInbox.MAX_SEEN, MessageInbox.parseSeen(big.serializeSeen()).size)
        assertEquals(400L, big.ackValue())
    }

    @Test fun garbageSeenStringIsIgnored() {
        assertEquals(listOf(1L, 2L), MessageInbox.parseSeen("1,x,2,,"))
        assertTrue(MessageInbox.parseSeen(null).isEmpty())
    }
}

class NotifyPolicyTest {
    private fun m(cat: String) = Message(1, cat, "Nori", "t", 0, null)

    @Test fun quietHoursLetOnlyRemindersAndApprovalsThrough() {
        assertTrue(NotifyPolicy.decide(m("reminder"), true, true, quiet = true, paused = false))
        assertTrue(NotifyPolicy.decide(m("approval"), true, true, quiet = true, paused = false))
        for (c in listOf("chat", "ping", "job", "peer", "mystery")) assertFalse("$c in quiet hours", NotifyPolicy.decide(m(c), true, true, quiet = true, paused = false))
        for (c in listOf("chat", "ping", "job", "peer", "reminder", "approval")) assertTrue("$c outside quiet hours", NotifyPolicy.decide(m(c), true, true, quiet = false, paused = false))
    }

    @Test fun eitherKeyOffOrPausedMeansNoNotification() {
        assertFalse(NotifyPolicy.decide(m("reminder"), false, true, false, false))
        assertFalse(NotifyPolicy.decide(m("reminder"), true, false, false, false))
        assertFalse(NotifyPolicy.decide(m("approval"), true, true, false, true))
    }
}

class CommandTrackerTest {
    private fun cmd(id: Long, approved: Boolean = false) = Command(id, "open_url", JSONObject(), approved, false)

    @Test fun aCommandResentAfterTheAckRunsOnlyOnce() {
        val t = CommandTracker()
        assertEquals(CommandTracker.Verdict.NEW, t.begin(cmd(77)))
        assertEquals(CommandTracker.Verdict.IN_FLIGHT, t.begin(cmd(77)))        // the server redelivers because it has not heard the ack yet
        t.finish(cmd(77))
        assertEquals(CommandTracker.Verdict.DONE, t.begin(cmd(77)))             // and again after it finished
        assertEquals(CommandTracker.Verdict.DONE, t.begin(cmd(77)))
    }

    @Test fun theApprovedResendOfANeedsApprovalCommandIsADifferentExecution() {
        val t = CommandTracker()
        assertEquals(CommandTracker.Verdict.NEW, t.begin(cmd(78, approved = false)))
        t.finish(cmd(78, approved = false))
        assertEquals(CommandTracker.Verdict.NEW, t.begin(cmd(78, approved = true)))
        t.finish(cmd(78, approved = true))
        assertEquals(CommandTracker.Verdict.DONE, t.begin(cmd(78, approved = true)))
    }

    @Test fun differentIdsAreIndependentAndMemoryIsBounded() {
        val t = CommandTracker(capacity = 3)
        for (i in 1L..5L) assertEquals(CommandTracker.Verdict.NEW, t.begin(cmd(i)))
        assertEquals(CommandTracker.Verdict.NEW, t.begin(cmd(1)))              // 1 was forgotten long ago
        assertEquals(CommandTracker.Verdict.IN_FLIGHT, t.begin(cmd(5)))
    }
}

class PollPolicyTest {
    @Test fun statusCodesMapToActions() {
        assertEquals(PollAction.OK, PollPolicy.classify(200))
        assertEquals(PollAction.OK, PollPolicy.classify(204))
        assertEquals(PollAction.RE_PAIR, PollPolicy.classify(401))
        assertEquals(PollAction.BACK_OFF_LONG, PollPolicy.classify(403))
        assertEquals(PollAction.SLOW_DOWN, PollPolicy.classify(429))
        for (c in listOf(0, 400, 404, 500, 502, 503)) assertEquals(PollAction.RETRY, PollPolicy.classify(c))
    }

    @Test fun timingsMatchTheContract() {
        assertEquals(40_000, PollPolicy.CLIENT_TIMEOUT_MS)
        assertEquals(25_000L, PollPolicy.SERVER_HOLD_MS)
        assertEquals(60_000L, PollPolicy.FORBIDDEN_WAIT_MS)
    }

    @Test fun backoffDoublesFromTwoSecondsToAMinuteAndResets() {
        val b = Backoff()
        assertEquals(listOf(2_000L, 4_000L, 8_000L, 16_000L, 32_000L, 60_000L, 60_000L), List(7) { b.failure() })
        b.success()
        assertEquals(2_000L, b.failure())
    }

    @Test fun waitAfterEachOutcome() {
        val b = Backoff()
        assertEquals(0L, b.wait(PollAction.OK))
        assertEquals(60_000L, b.wait(PollAction.BACK_OFF_LONG))
        assertEquals(30_000L, b.wait(PollAction.SLOW_DOWN))
        assertEquals(2_000L.coerceAtLeast(0), Backoff().wait(PollAction.RETRY))
        assertNotNull(PollAction.valueOf("RE_PAIR"))
    }
}
