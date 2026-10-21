package app.nori.companion

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.ComponentName
import android.content.Context
import android.graphics.Path
import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import app.nori.companion.core.AppGuard
import app.nori.companion.core.Approval
import app.nori.companion.core.FieldInfo
import app.nori.companion.core.Risk
import app.nori.companion.core.Sensitive
import org.json.JSONArray
import org.json.JSONObject

/** What a command did, in a form the caller turns into a /device/result body. */
sealed class Outcome {
    class Ok(val result: JSONObject) : Outcome()
    class Refused(val error: String) : Outcome()
    /** Nothing was done: the owner is asked first. */
    class NeedsApproval(val reason: String, val describe: String, val fingerprint: String) : Outcome()
}

/**
 * Nori's eyes and (only inside the owner's control window) hands on the screen.
 *
 * It does nothing between requests: no events are watched, nothing is recorded. READING and ACTING both pass the same gate the caller already applied
 * (switches, window, pause, hourly cap) AND are re-checked here against the app actually in front, using the guard function supplied by the caller.
 * Password / card / one-time-code fields are reported as "(sensitive field)" and never read or typed into. Risky controls return needs_approval and nothing is done.
 */
class NoriAccessibilityService : AccessibilityService() {
    private class Rec(
        val node: AccessibilityNodeInfo, val pkg: String, val role: String, val label: String, val text: String?, val editable: Boolean, val clickable: Boolean,
        val sensitive: Boolean, val field: FieldInfo, val viewId: String, val occurrence: Int, val fp: String,
    )

    @Volatile private var refs: List<Rec> = emptyList()

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        SentLog.add(this, "accessibility service connected")
    }

    override fun onUnbind(intent: android.content.Intent?): Boolean {
        if (instance === this) instance = null
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) { /* nothing is watched: it only answers requests */ }
    override fun onInterrupt() {}

    /** The package of the window in front, or null when none is readable (screen off, locked, transition). */
    fun foregroundPackage(): String? = try { rootInActiveWindow?.packageName?.toString() } catch (e: Exception) { null }

    // ── describing nodes ─────────────────────────────────────────────
    private fun role(n: AccessibilityNodeInfo): String {
        val c = n.className?.toString() ?: ""
        return when {
            c.endsWith("Button") || c.endsWith("ImageButton") -> "button"
            n.isEditable || c.endsWith("EditText") -> "field"
            c.endsWith("Switch") || c.endsWith("CheckBox") || c.endsWith("RadioButton") || c.endsWith("ToggleButton") -> "switch"
            c.endsWith("ImageView") -> "image"
            c.endsWith("WebView") -> "web"
            c.endsWith("RecyclerView") || c.endsWith("ListView") || c.endsWith("ScrollView") -> "list"
            c.endsWith("TextView") -> "text"
            else -> if (n.isClickable) "button" else "item"
        }
    }

    private fun clean(s: CharSequence?): String = (s?.toString() ?: "").replace(Regex("\\s+"), " ").trim().take(200)

    private fun hintOf(n: AccessibilityNodeInfo): String {
        val parts = ArrayList<String>()
        if (Build.VERSION.SDK_INT >= 26) n.hintText?.let { parts.add(clean(it)) }
        n.contentDescription?.let { parts.add(clean(it)) }
        try { n.labeledBy?.let { parts.add(clean(it.text ?: it.contentDescription)) } } catch (e: Exception) { /* no label node */ }
        return parts.filter { it.isNotEmpty() }.joinToString(" ")
    }

    private fun makeRec(n: AccessibilityNodeInfo, fallbackPkg: String, occurrence: Int): Rec {
        val editable = n.isEditable
        val viewId = n.viewIdResourceName ?: ""
        val text = clean(n.text)
        val hint = if (editable) hintOf(n) else clean(n.contentDescription)
        val field = FieldInfo(isPassword = n.isPassword, inputType = n.inputType, editable = editable, className = n.className?.toString() ?: "", viewId = viewId, hint = hint, text = text)
        val sensitive = Sensitive.isSensitive(field)
        val label = if (sensitive) Sensitive.MASK else if (editable) hint.ifEmpty { "text field" } else text.ifEmpty { clean(n.contentDescription) }
        val role = role(n)
        val pkg = n.packageName?.toString() ?: fallbackPkg
        val shownText = if (editable && !sensitive && text.isNotEmpty()) text else null
        return Rec(n, pkg, role, label, shownText, editable, n.isClickable, sensitive, field, viewId, occurrence, Approval.fingerprint(pkg, role, label, viewId, occurrence))
    }

    private fun walkAll(root: AccessibilityNodeInfo, pkg: String, maxNodes: Int): Pair<List<Rec>, Boolean> {
        val out = ArrayList<Rec>()
        val seen = HashMap<String, Int>()
        var visited = 0
        var truncated = false
        fun walk(n: AccessibilityNodeInfo, depth: Int) {
            if (visited++ > 1200 || depth > 40) { truncated = true; return }
            if (!n.isVisibleToUser) return
            val hasWords = !n.text.isNullOrBlank() || !n.contentDescription.isNullOrBlank()
            if (hasWords || n.isClickable || n.isEditable || n.isScrollable || n.isCheckable) {
                if (out.size >= maxNodes) { truncated = true } else {
                    val provisional = makeRec(n, pkg, 0)
                    val k = provisional.role + "|" + provisional.label + "|" + provisional.viewId
                    val occ = seen[k] ?: 0
                    seen[k] = occ + 1
                    out.add(if (occ == 0) provisional else makeRec(n, pkg, occ))
                }
            }
            for (i in 0 until n.childCount) n.getChild(i)?.let { walk(it, depth + 1) }
        }
        walk(root, 0)
        return Pair(out, truncated)
    }

    private fun appLabel(pkg: String): String = try { packageManager.getApplicationLabel(packageManager.getApplicationInfo(pkg, 0)).toString() } catch (e: Exception) { pkg }

    // ── reading ──────────────────────────────────────────────────────
    /** The numbered controls of the window in front. [guard] is applied to the package of the window actually read. */
    fun readScreen(guard: (String?) -> AppGuard.Verdict, maxNodes: Int = 350): Outcome {
        val root = rootInActiveWindow ?: return Outcome.Refused("no window is readable right now (the screen may be off or locked)")
        val pkg = root.packageName?.toString() ?: ""
        val v = guard(pkg)
        if (!v.allowed) return Outcome.Refused(v.reason)
        val (recs, truncated) = walkAll(root, pkg, maxNodes)
        refs = recs
        val nodes = JSONArray()
        recs.forEachIndexed { i, r ->
            val j = JSONObject().put("ref", "n$i").put("role", r.role).put("label", r.label).put("editable", r.editable).put("clickable", r.clickable)
            r.text?.let { j.put("text", it) }
            nodes.put(j)
        }
        return Outcome.Ok(JSONObject().put("app", appLabel(pkg)).put("package", pkg).put("nodes", nodes).put("truncated", truncated))
    }

    // ── acting ───────────────────────────────────────────────────────
    private fun clickableAncestor(n: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        var cur: AccessibilityNodeInfo? = n
        var hops = 0
        while (cur != null && hops++ < 8) { if (cur.isClickable) return cur; cur = cur.parent }
        return null
    }

    /** What is written on a control or on the thing it sits in, for the risk check: its own words, its clickable parent's, and a few of their children's. */
    private fun labelsAround(n: AccessibilityNodeInfo): Pair<List<String>, String> {
        val out = ArrayList<String>()
        val ids = StringBuilder()
        fun add(x: AccessibilityNodeInfo) {
            if (x.isPassword) return
            clean(x.text).takeIf { it.isNotEmpty() }?.let { out.add(it) }
            clean(x.contentDescription).takeIf { it.isNotEmpty() }?.let { out.add(it) }
            x.viewIdResourceName?.let { ids.append(' ').append(it) }
        }
        fun kids(x: AccessibilityNodeInfo, depth: Int) {
            if (depth > 3 || out.size > 12) return
            for (i in 0 until x.childCount) x.getChild(i)?.let { add(it); kids(it, depth + 1) }
        }
        add(n)
        val anc = clickableAncestor(n)
        if (anc != null && anc != n) add(anc)
        kids(anc ?: n, 0)
        return Pair(out, ids.toString())
    }

    private fun gesture(path: Path, ms: Long): Boolean = dispatchGesture(GestureDescription.Builder().addStroke(GestureDescription.StrokeDescription(path, 0, ms)).build(), null, null)

    private fun swipe(direction: String): Boolean {
        val r = Rect()
        rootInActiveWindow?.getBoundsInScreen(r)
        val cx = r.exactCenterX(); val cy = r.exactCenterY(); val dx = r.width() * 0.3f; val dy = r.height() * 0.3f
        val p = Path()
        when (direction) {
            "down" -> { p.moveTo(cx, cy + dy); p.lineTo(cx, cy - dy) }          // content moves up: reveals what is below
            "up" -> { p.moveTo(cx, cy - dy); p.lineTo(cx, cy + dy) }
            "left" -> { p.moveTo(cx - dx, cy); p.lineTo(cx + dx, cy) }
            else -> { p.moveTo(cx + dx, cy); p.lineTo(cx - dx, cy) }
        }
        return gesture(p, 300)
    }

    private class Refusal(message: String) : Exception(message)

    private fun parseRef(ref: String): Int = ref.trim().removePrefix("n").toIntOrNull() ?: throw Refusal("name a control from your last read_screen (for example n7)")

    /**
     * The element a ref points at, re-resolved against the live screen. With an `_expect` fingerprint (the approved re-send) the element must still carry that fingerprint:
     * the same ref first, then any unique match in the last read, then in a fresh read. Anything else means the screen changed and nothing is done.
     */
    private fun resolve(ref: String, expect: String, pkg: String): Rec {
        val idx = parseRef(ref)
        val old = refs.getOrNull(idx)
        val rebuilt = old?.takeIf { it.node.refresh() }?.let { makeRec(it.node, pkg, it.occurrence) }
        if (expect.isEmpty()) return rebuilt ?: throw Refusal("the screen changed or that control is not in your last read: read the screen again")
        if (rebuilt != null && rebuilt.fp == expect) return rebuilt
        val live = refs.filter { it.node.refresh() }.map { makeRec(it.node, pkg, it.occurrence) }.filter { it.fp == expect }
        if (live.size == 1) return live[0]
        val root = rootInActiveWindow
        if (root != null) {
            val freshAll = walkAll(root, pkg, 350).first
            val fresh = freshAll.filter { it.fp == expect }
            if (fresh.size == 1) { refs = freshAll; return fresh[0] }
        }
        throw Refusal("the screen changed since approval was asked, so nothing was done: read it again")
    }

    /** Does one step. Returns Ok with what was done and the screen now, NeedsApproval (nothing done), or Refused. */
    fun interact(args: JSONObject, approved: Boolean, forceApproval: Boolean, guard: (String?) -> AppGuard.Verdict): Outcome {
        return try { doInteract(args, approved, forceApproval, guard) } catch (r: Refusal) { Outcome.Refused(r.message ?: "refused") }
    }

    private fun doInteract(args: JSONObject, approved: Boolean, force: Boolean, guard: (String?) -> AppGuard.Verdict): Outcome {
        val op = args.optString("action").trim().lowercase()
        if (op !in OPS) throw Refusal("unknown interact action \"${op.take(20)}\": use tap, type, scroll, back, home, recents, enter or wait")
        val root = rootInActiveWindow ?: throw Refusal("no window is readable right now (the screen may be off or locked)")
        val pkg = root.packageName?.toString() ?: ""
        val v = guard(pkg)
        if (!v.allowed) throw Refusal(v.reason)
        val expect = args.optString("_expect", "")

        // 1. find the target and judge it
        var target: Rec? = null
        val describe: String
        val fp: String
        val risk: Risk
        when (op) {
            "tap" -> {
                val t = resolve(args.optString("ref"), expect, pkg)
                target = t
                val (labels, ids) = labelsAround(t.node)
                risk = Approval.classifyTap(labels + t.label, t.viewId + " " + ids)
                describe = Approval.describe(t.role, if (t.sensitive) "" else t.label, t.viewId); fp = t.fp
            }
            "type" -> {
                val text = args.optString("text", "")
                if (text.isEmpty()) throw Refusal("type needs `text`")
                if (Sensitive.textIsSecret(text)) throw Refusal("this looks like a card number, so it is never typed")
                val t = if (args.has("ref")) resolve(args.optString("ref"), expect, pkg) else focusedRec(pkg)
                if (t.sensitive) throw Refusal("that is a password, card or one-time-code field: Nori never types into those")
                if (!t.editable) throw Refusal("that is not a text field")
                target = t
                risk = Approval.classifyType(text, t.field)
                describe = Approval.describe("field", t.label, t.viewId); fp = t.fp
            }
            "scroll" -> {
                val t = if (args.has("ref")) resolve(args.optString("ref"), expect, pkg) else null
                target = t
                risk = Risk.SAFE
                describe = if (t != null) Approval.describe(t.role, t.label, t.viewId) else "scroll"; fp = t?.fp ?: Approval.fingerprint(pkg, "action", "scroll", "", 0)
            }
            "enter" -> {
                val t = focusedRec(pkg)
                if (t.sensitive) throw Refusal("the focused field is a password, card or one-time-code field")
                target = t
                risk = Approval.classifyEnter(t.field)
                describe = Approval.describe("field", t.label, t.viewId); fp = t.fp
            }
            else -> { risk = Risk.SAFE; describe = "action \"$op\""; fp = Approval.fingerprint(pkg, "action", op, "", 0) }
        }

        // 2. approval: nothing is done until the owner said yes, and the thing they said yes to must still be there
        if (!approved) {
            if (force) return Outcome.NeedsApproval("every step is being checked with you on this phone", describe, fp)
            if (risk.needsApproval) return Outcome.NeedsApproval(risk.reason, describe, fp)
        } else {
            if (expect.isEmpty()) throw Refusal("an approved step must carry the element fingerprint, so it was not done")
            if (fp != expect) throw Refusal("the screen changed since approval was asked, so nothing was done: read it again")
        }

        // 3. do it
        val did: String
        when (op) {
            "tap" -> {
                val t = target!!
                val clickTarget = clickableAncestor(t.node)
                val ok = if (clickTarget != null) clickTarget.performAction(AccessibilityNodeInfo.ACTION_CLICK) else { val r = Rect(); t.node.getBoundsInScreen(r); gesture(Path().apply { moveTo(r.exactCenterX(), r.exactCenterY()) }, 60) }
                if (!ok) throw Refusal("the tap did not go through")
                did = "tapped " + describe
            }
            "type" -> {
                val raw = args.optString("text", "")
                val pressEnter = raw.endsWith("\n") || raw.endsWith("\r")
                val text = raw.trimEnd('\n', '\r')
                val b = Bundle().apply { putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text) }
                if (!target!!.node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, b)) throw Refusal("the text did not go in")
                if (pressEnter) pressImeEnter(target.node)
                did = "typed ${text.length} characters into $describe" + (if (pressEnter) " and pressed Enter" else "")
            }
            "scroll" -> {
                val dir = args.optString("direction", "down").lowercase()
                if (dir !in setOf("up", "down", "left", "right")) throw Refusal("direction must be up, down, left or right")
                val scroller = target?.node?.takeIf { it.isScrollable } ?: refs.map { it.node }.firstOrNull { it.isScrollable && it.refresh() }
                val action = when (dir) { "down", "right" -> AccessibilityNodeInfo.ACTION_SCROLL_FORWARD; else -> AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD }
                if (!(scroller?.performAction(action) == true || swipe(dir))) throw Refusal("it would not scroll")
                did = "scrolled $dir"
            }
            "enter" -> { pressImeEnter(target!!.node); did = "pressed Enter in $describe" }
            "back" -> { performGlobalAction(GLOBAL_ACTION_BACK); did = "went back" }
            "home" -> { performGlobalAction(GLOBAL_ACTION_HOME); did = "went to the home screen" }
            "recents" -> { performGlobalAction(GLOBAL_ACTION_RECENTS); did = "opened recent apps" }
            "wait" -> { Thread.sleep(args.optLong("ms", 1000).coerceIn(100, 5000)); did = "waited" }
            else -> throw Refusal("unknown action")
        }
        Thread.sleep(700)
        val result = JSONObject().put("ok", true).put("did", did)
        when (val screen = readScreen(guard, 90)) {
            is Outcome.Ok -> for (k in screen.result.keys()) result.put(k, screen.result.get(k))
            is Outcome.Refused -> result.put("app", "").put("nodes", JSONArray()).put("note", "the screen now showing is not shown: " + screen.error)
            else -> {}
        }
        return Outcome.Ok(result)
    }

    private fun focusedRec(pkg: String): Rec {
        val f = findFocus(AccessibilityNodeInfo.FOCUS_INPUT) ?: throw Refusal("no field is focused: tap one first")
        return makeRec(f, pkg, 0)
    }

    private fun pressImeEnter(n: AccessibilityNodeInfo) {
        if (Build.VERSION.SDK_INT < 30) throw Refusal("pressing Enter needs Android 11 or newer")
        if (!n.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id)) throw Refusal("Enter did not go through")
    }

    companion object {
        @Volatile var instance: NoriAccessibilityService? = null
        val OPS = setOf("tap", "type", "scroll", "back", "home", "recents", "enter", "wait")

        fun isEnabled(c: Context): Boolean {
            val flat = Settings.Secure.getString(c.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: return false
            val mine = ComponentName(c, NoriAccessibilityService::class.java)
            return flat.split(":").any { ComponentName.unflattenFromString(it) == mine }
        }

        /** Running = enabled in Android's settings AND connected. */
        fun running(c: Context): Boolean = instance != null && isEnabled(c)
    }
}
