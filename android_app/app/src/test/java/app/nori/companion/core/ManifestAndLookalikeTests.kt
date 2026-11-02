package app.nori.companion.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/** Guards the promises the app makes about itself: what it asks Android for, and that look-alike spellings do not slip past the risk check. */
class ManifestTest {
    private val manifest: String by lazy {
        val candidates = listOf(File("src/main/AndroidManifest.xml"), File("app/src/main/AndroidManifest.xml"))
        candidates.first { it.exists() }.readText()
    }

    @Test fun noMonitoringPermissionsAreRequested() {
        for (p in listOf("QUERY_ALL_PACKAGES", "PACKAGE_USAGE_STATS", "CAMERA", "READ_SMS", "SEND_SMS", "READ_CONTACTS", "READ_CALL_LOG", "ACTIVITY_RECOGNITION", "FOREGROUND_SERVICE_LOCATION")) {
            assertFalse("manifest must not request $p", manifest.contains("<uses-permission android:name=\"android.permission.$p\""))
            assertFalse("manifest must not mention android.permission.$p at all", manifest.contains("android.permission.$p\""))
        }
    }

    @Test fun notificationAccessIsDeclaredWithTheBindPermission() {
        val svc = manifest.substringAfter("android:name=\".NoriNotificationListener\"").substringBefore("</service>")
        assertTrue(svc.contains("android:permission=\"android.permission.BIND_NOTIFICATION_LISTENER_SERVICE\""))
        assertTrue(svc.contains("android.service.notification.NotificationListenerService"))
        assertTrue("the system must be able to bind it", svc.contains("android:exported=\"true\""))
    }

    @Test fun incomingTextsAreReceiveOnlyAndTheReceiverIsLockedToTheSystem() {
        assertTrue(manifest.contains("<uses-permission android:name=\"android.permission.RECEIVE_SMS\""))
        val rcv = manifest.substringAfter("android:name=\".SmsReceiver\"").substringBefore("</receiver>")
        assertTrue(rcv.contains("android:permission=\"android.permission.BROADCAST_SMS\""))
        assertTrue(rcv.contains("android.provider.Telephony.SMS_RECEIVED"))
        assertTrue(rcv.contains("android:exported=\"true\""))
        assertFalse("texts are not read or sent", manifest.contains("android.permission.READ_SMS\"") || manifest.contains("android.permission.SEND_SMS\""))
    }

    @Test fun sharingIsOffByDefaultInThePureRules() {
        assertEquals(CaptureLevel.OFF, CaptureLevel.parse(null)); assertEquals(CaptureLevel.OFF, CaptureLevel.parse(""))
        val none = SlotCapture(0, true, CaptureLevel.ALL, CaptureLevel.OFF, emptyList(), emptyList())
        assertFalse(Capture.anyWanted(listOf(none))); assertFalse(Capture.anyWantedSms(listOf(none), true))
        assertEquals(CaptureLevel.OFF, DeviceSettings().capture); assertFalse(DeviceSettings().captureSms)
    }

    @Test fun cleartextAndBackupAreOff() {
        assertTrue(manifest.contains("android:usesCleartextTraffic=\"false\""))
        assertTrue(manifest.contains("android:allowBackup=\"false\""))
    }

    @Test fun theForegroundServiceIsSpecialUseOnly() {
        val svc = manifest.substringAfter("android:name=\".SyncService\"").substringBefore(">")
        assertTrue(svc.contains("foregroundServiceType=\"specialUse\""))
        assertFalse(svc.contains("location"))
    }

    @Test fun deepLinksAndShareEntryPointsExist() {
        assertTrue(manifest.contains("android:scheme=\"nori-pair\""))
        assertTrue(manifest.contains("android:scheme=\"nori-app\" android:host=\"settings\""))
        assertTrue(manifest.contains("android.intent.action.SEND"))
        assertTrue(manifest.contains("android.service.quicksettings.action.QS_TILE"))
        assertTrue(manifest.contains("android.appwidget.action.APPWIDGET_UPDATE"))
    }

    @Test fun internalComponentsAreNotExported() {
        for (c in listOf(".SyncService", ".ScreenShareService", ".ApprovalReceiver", ".GeofenceReceiver", ".ControlReceiver", ".QuickTaskActivity", ".ScreenShareRequestActivity", ".ApprovalActivity")) {
            val tag = manifest.substringAfter("android:name=\"$c\"").substringBefore(">")
            assertTrue("$c must not be exported", tag.contains("android:exported=\"false\""))
        }
    }
}

class LookalikeApprovalTest {
    @Test fun fullWidthAndAccentedSpellingsAreStillRisky() {
        assertTrue(Approval.classifyTap(listOf("Ｓｅｎｄ"), "").needsApproval)         // Ｓｅｎｄ
        assertTrue(Approval.classifyTap(listOf("Sénd"), "").needsApproval)                         // Sénd
        assertTrue(Approval.classifyTap(listOf("DELETE"), "").needsApproval)
        assertTrue(Approval.classifyTap(listOf("d e l e t e".replace(" ", "")), "").needsApproval)
        assertFalse(Approval.classifyTap(listOf("Séarch"), "").needsApproval)
    }
}
