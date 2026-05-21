You are writing a note to your future self.

A single code change was too large to read at once, so it was split into chunks and each chunk described separately. The notes below are those per-chunk descriptions — together they cover one change. Merge them into a single note.

Your future readers are LLM agents — most often you — pulling this back as evidence for downstream features: rationale generation, code review, change attribution, dependency analysis, search. No human reads this directly.

Two anchors:
- Token efficiency. Every word costs your future self's context budget. Don't pad. Don't enumerate the chunks. Don't moralize.
- No ambiguity. Your future self will have neither the diff nor the per-chunk notes — only your merged note. Preserve every identity (names, paths, signatures) the chunk notes carry.

Write one unified description, as if you had read the whole change at once. You choose the shape, density, and notation. There is no required schema.

Chunk descriptions:
{{DESCRIPTIONS}}

Output only the merged description.
