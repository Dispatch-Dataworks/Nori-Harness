package app.nori.companion.core

/** How the app refers to the assistant(s) it is paired with: by the name each server reports ("Iron"), never a hard-coded "Nori" (that is the product). */
object AgentName {
    const val NONE = "your assistant"

    /** One name, "A and B", or "A, B and C"; names that repeat (case aside) count once; none at all reads "your assistant". */
    fun join(names: List<String>): String {
        val seen = LinkedHashMap<String, String>()
        for (n in names) { val t = n.trim(); if (t.isNotEmpty()) seen.putIfAbsent(t.lowercase(), t) }
        val d = seen.values.toList()
        return when (d.size) {
            0 -> NONE
            1 -> d[0]
            2 -> "${d[0]} and ${d[1]}"
            else -> d.dropLast(1).joinToString(", ") + " and " + d.last()
        }
    }

    /** For the start of a sentence ("your assistant" -> "Your assistant"; "iron" stays as the server wrote it apart from the first letter). */
    fun cap(s: String): String = s.replaceFirstChar { it.uppercase() }
}
