# Grounding Writer Prompts

- `system.md`: runtime English prompt, loaded by `llm_writer.load_system_prompt()`.
  Translates the original paragraph by paragraph, preserving the 11 requirements,
  writing guidance, examples, output fields and 2–4 sentence guidance.
- `system.original.zh.md`: preserved original Chinese/mixed-language prompt for
  comparison only. Not loaded by generation. Historical city-language instructions
  in this file are not the current generation policy.

Confirmed semantic differences from the original (2026-10-12):

1. Formal generated prose is English for all cities (opening language contract,
   requirement 5 and final writing-guidance bullet).
2. Frozen non-English POI names may be quoted verbatim without translation or new
   aliases (opening contract and requirement 10). Ordinary prose/categories use
   English; the enum warning does not prohibit natural English category words.
3. Review translation stays separate from tested-agent input (contract and item 5).

2026-10-13 confirmed image contract: Writer sees the final task image first and
all valid reference views with per-attachment roles and evidence indices. Only
the final image is sent to the tested agent. Visual clues come from that image;
reference-derived information used as clues must appear in the user request.
`system.current.zh.md` is the current Chinese reading counterpart, not a runtime prompt.
The Chinese original is retained unchanged apart from line-ending normalization.
