You are writing a note to your future self.

A single code change was too large to read at once, so it was split into chunks and each chunk described separately. You are given those per-chunk descriptions and must merge them into one unified note, as if you had read the whole change at once.

Your future readers are LLM agents — most often you — pulling this back as evidence for downstream features: rationale generation, code review, change attribution, dependency analysis, search. No human reads this directly.

Two anchors:
- Token efficiency. Every word costs your future self's context budget. Don't pad. Don't enumerate the chunks. Don't moralize.
- No ambiguity. Your future self will have neither the diff nor the per-chunk notes — only your merged note. Preserve every identity (names, paths, signatures) the chunk notes carry.

You choose the shape, density, and notation. There is no required schema.

Output only the merged description — no preamble, no fences.
