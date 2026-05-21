You are writing a note to your future self.

The diff below describes a code change. Your future readers are LLM agents — most often you — pulling this back as evidence for downstream features: rationale generation, code review, change attribution, dependency analysis, search. No human reads this directly.

Two anchors:
- Token efficiency. Every word costs your future self's context budget. Don't pad. Don't restate the diff verbatim. Don't moralize.
- No ambiguity. Your future self will not have the diff. They must be able to reason about this change from your note alone — paraphrases that erase identity are a failure mode.

You choose the shape, density, and notation. There is no required schema. Decide what's worth keeping and how to write it.

Diff:
{{DIFF}}

Output only the description.
