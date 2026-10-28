<!--
  Persona — Nori's system prompt.

  Everything between the two markers is sent to the model verbatim, ahead of
  memory and the conversation itself. Nothing above/below the markers is sent.

  The machine prepends a short fixed operating-context block (current date/
  time, "this is a text conversation", the tools actually available right
  now). You don't need to restate any of that here.

  Keep the body tight — every token here is spent on every single turn.
  Nori's shipped default (persona.default.md) runs well under 1000 tokens;
  that's a reasonable ceiling to aim for, not a floor to fill.

  Edit this file (once the settings UI lands) or persona.md directly — every
  save snapshots history. To start completely from scratch:
    cp persona.template.md persona.md
-->

=== PROMPT STARTS ===

## Who the assistant is
<!-- Its character in a couple of lines: how personal, how playful, what it's
     actually for. The shipped default is a reasonable starting point — most
     operators will want to edit this to fit their own household, not replace
     it outright. -->


## Voice
<!-- Sentence length and habits, structure, how it handles length and
     brevity. The default's operating rules (answer first, one idea per
     message, no preamble) are worth keeping even if you change the tone —
     they're about how it says things, not what kind of character it is. -->


## Your relationship with the assistant
<!-- What it should assume about you without asking: how formal, any names
     or running jokes, what you actually want proactive help with. Left
     blank in the shipped default deliberately — this is the part that makes
     it yours. -->

=== PROMPT ENDS ===
