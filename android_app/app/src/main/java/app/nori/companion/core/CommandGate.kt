package app.nori.companion.core

/** The owner's own switches on the phone (the device half of every two-key capability). All off until the owner turns them on. */
data class OwnerSwitches(
    val notifications: Boolean = true,
    val share: Boolean = true,
    val health: Boolean = false,
    val places: Boolean = false,
    val actions: Boolean = false,
    val screen: Boolean = false,
    val control: Boolean = false,
)

/** Everything the gate needs to know, passed in so the decision is a pure function. */
class GateState(
    val now: Long,
    val paused: Boolean,
    val server: DeviceSettings,
    val owner: OwnerSwitches,
    val interactUntil: Long,
    val shareUntil: Long,
    val accessibilityRunning: Boolean,
    val projectionRunning: Boolean,
)

/** Decides, before anything touches the phone, whether a server command may run at all. A non-null result is the specific refusal text for `{ok:false,error}`. */
object CommandGate {
    val ACTION_COMMANDS: Set<String> = setOf("open_url", "open_app", "set_timer", "set_alarm", "navigate", "list_apps")
    val CONTROL_COMMANDS: Set<String> = setOf("read_screen", "interact")
    const val SCREENSHOT = "screenshot"
    val ALL: Set<String> = ACTION_COMMANDS + CONTROL_COMMANDS + SCREENSHOT

    /** Commands that change something on the phone and therefore count against the hourly cap. read_screen, screenshot and list_apps only look. */
    val COUNTED: Set<String> = setOf("open_url", "open_app", "set_timer", "set_alarm", "navigate")

    /** interact steps (tap, type, scroll...) have their own, larger, budget: this many times the actions-per-hour setting. */
    const val INTERACT_CAP_FACTOR = 4

    fun interactLimit(actionsPerHour: Int): Int = if (actionsPerHour <= 0) 0 else actionsPerHour.toLong().times(INTERACT_CAP_FACTOR).coerceAtMost(10_000).toInt()

    fun check(action: String, st: GateState): String? {
        if (action !in ALL) return "the phone does not know the action \"${action.take(40)}\""
        if (st.paused) return "this phone is paused by its owner"
        when (action) {
            in ACTION_COMMANDS -> {
                if (!st.server.actions) return "phone actions are switched off on the server"
                if (!st.owner.actions) return "the owner has not allowed phone actions in the Nori app"
            }
            in CONTROL_COMMANDS -> {
                if (!st.server.control) return "phone control is switched off on the server"
                if (!st.owner.control) return "the owner has not allowed phone control in the Nori app"
                if (!st.accessibilityRunning) return "the Nori accessibility service is not running (it must be turned on in Android settings)"
                if (!Windows.isOpen(st.interactUntil, st.now)) return "the interact window is closed: the phone's owner opens it on the phone (Nori app or Quick Settings tile)"
            }
            SCREENSHOT -> {
                if (!st.server.screen) return "screen sharing is switched off on the server"
                if (!st.owner.screen) return "the owner has not allowed screen sharing in the Nori app"
                if (!Windows.isOpen(st.shareUntil, st.now)) return "the screen-share window is closed: the phone's owner opens it on the phone"
                if (!st.projectionRunning) return "screen capture is not running on the phone"
            }
        }
        return null
    }

    /** The foreground-app check applies to every command that looks at or acts on the screen. */
    fun needsForegroundCheck(action: String) = action in CONTROL_COMMANDS || action == SCREENSHOT

    /** The device's own half of the capabilities, as reported in `caps`. */
    fun deviceCaps(owner: OwnerSwitches, accessibilityRunning: Boolean, projectionReady: Boolean, notificationsPermitted: Boolean, healthReady: Boolean, placesReady: Boolean): Caps = Caps(
        notifications = owner.notifications && notificationsPermitted,
        share = owner.share,
        health = owner.health && healthReady,
        places = owner.places && placesReady,
        actions = owner.actions,
        screen = owner.screen && projectionReady,
        control = owner.control && accessibilityRunning,
    )
}
