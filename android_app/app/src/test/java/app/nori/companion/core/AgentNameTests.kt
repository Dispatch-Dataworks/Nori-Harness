package app.nori.companion.core

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** The app names the assistant the server names ("Iron"), not a fixed "Nori" (2026-10-30, operator's report: "message nori", "turn on notifications for nori"). */
class AgentNameTests {
    @Test fun oneNameIsThatName() = assertEquals("Iron", AgentName.join(listOf("Iron")))

    @Test fun twoNamesJoinWithAnd() = assertEquals("Nori and Iron", AgentName.join(listOf("Nori", "Iron")))

    @Test fun threeNamesGetCommas() = assertEquals("Nori, Iron and Tin", AgentName.join(listOf("Nori", "Iron", "Tin")))

    @Test fun aNameThatRepeatsCountsOnce() {
        assertEquals("Iron", AgentName.join(listOf("Iron", "iron", " IRON ")))
        assertEquals("Nori and Iron", AgentName.join(listOf("Nori", "Iron", "nori")))
    }

    @Test fun noNameAtAllReadsAsYourAssistantNotAsNori() {
        assertEquals("your assistant", AgentName.join(emptyList()))
        assertEquals("your assistant", AgentName.join(listOf("", "  ")))
        assertFalse(AgentName.join(emptyList()).contains("Nori"))
    }

    @Test fun capitalisingOnlyTouchesTheFirstLetter() {
        assertEquals("Your assistant", AgentName.cap("your assistant"))
        assertEquals("Iron", AgentName.cap("Iron"))
        assertEquals("", AgentName.cap(""))
    }

    @Test fun theServerSendsItsAssistantsNameAndTheAppKeepsIt() {
        val s = DeviceSettings.fromJson(JSONObject("""{"assistant_name":"  Iron  ","notifications":true}"""))
        assertEquals("Iron", s.assistantName)
        assertEquals(s, DeviceSettings.parse(s.toJson().toString()))                          // stored and read back unchanged
        assertEquals("Iron", JSONObject(s.toJson().toString()).getString("assistant_name"))
    }

    @Test fun anOlderServerThatSendsNoNameLeavesItBlankSoTheAppFallsBack() {
        assertEquals("", DeviceSettings.fromJson(JSONObject("""{}""")).assistantName)
        assertEquals("", DeviceSettings.parse("not json").assistantName)
    }

    @Test fun aLongOrOddNameIsCapped() {
        val s = DeviceSettings.fromJson(JSONObject().put("assistant_name", "x".repeat(200)))
        assertEquals(40, s.assistantName.length)
    }

    @Test fun theStatusLineNamesTheAssistantTheCallerGivesIt() {
        val now = 1_000_000L
        val paused = StatusLine.build(true, false, true, now, 0, 0, "", "iron.example.com", "Iron")
        assertTrue(paused, paused.contains("Iron cannot do anything on this phone"))
        assertFalse(paused, paused.contains("Nori"))
        val open = StatusLine.build(true, false, false, now, now + 90_000, 0, "Connected", "iron.example.com", "Iron")
        assertTrue(open, open.contains("Iron may act on this phone"))
        assertFalse(open, open.contains("Nori"))
        val unnamed = StatusLine.build(true, false, false, now, now + 90_000, 0, "Connected", "x")
        assertTrue(unnamed, unnamed.contains("Your assistant may act on this phone"))
        assertFalse(StatusLine.build(false, false, false, now, 0, 0, "", "").contains("Nori's"))
    }

    @Test fun theNotificationChannelsDoNotNameNori() {
        for (spec in Channels.ALL) {
            assertFalse(spec.name, spec.name.contains("Nori"))
            if (spec.id != Channels.UPDATES) assertFalse(spec.description, spec.description.contains("Nori"))   // the updates channel is about the app itself
        }
        assertEquals("Assistant", Channels.titleFor(Message(1, "chat", "", "hi", 0, null)))
    }
}
