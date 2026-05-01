import { z } from 'zod/v4';
import type { CodeGraphNode } from '../codegraph/reader.js';
import type { EvidenceRecord } from '../evidence/store.js';

// Bump whenever SYSTEM_PROMPT, RationaleSchema, or buildUserPrompt changes
// in a way that should invalidate cached rationale.
export const PROMPT_VERSION = 'v1';

export const RationaleSchema = z.object({
  purpose: z
    .string()
    .describe('One sentence describing what this code does.'),
  why: z
    .string()
    .describe(
      'One paragraph explaining why this code exists — the historical or contextual rationale, not the mechanism. Cite relevant commits when supporting a claim.'
    ),
  constraints: z
    .array(z.string())
    .describe(
      'Things that must be preserved when modifying: invariants, contracts, dependencies on caller behaviour. Empty array if none are evidenced.'
    ),
  tradeoffs: z
    .array(z.string())
    .describe(
      'Notable design tradeoffs visible in the history. Empty array if none are evidenced.'
    ),
  risks: z
    .array(z.string())
    .describe(
      'Risks of modification: regressions, breaking changes for consumers, compliance or security implications. Empty array if none are evidenced.'
    ),
});

export type Rationale = z.infer<typeof RationaleSchema>;

export const SYSTEM_PROMPT = `You are an analyst that explains why code exists, not just what it does.

Given a code symbol's location and a bundle of evidence (commits, blame data, etc.), generate a structured rationale explaining the historical and contextual reasons for this code.

Guidelines:
- Be specific. Cite evidence (commit subjects or short SHAs) when supporting a claim.
- Be honest. If evidence is thin or unclear, say "Insufficient evidence" rather than speculating. Do not invent intent that the commits do not support.
- Prefer the language of the original commits over your own paraphrasing.
- For constraints / tradeoffs / risks: only include items you can defend from the evidence. An empty array is the correct answer when there's no signal.
- Keep each list entry to one or two sentences. Keep "purpose" to one sentence and "why" to one short paragraph.

Output a JSON object matching the provided schema. Do not include extra fields, prose outside the JSON, or markdown formatting.`;

export interface SymbolContext {
  node: CodeGraphNode;
}

export function buildUserPrompt(
  ctx: SymbolContext,
  evidence: EvidenceRecord[]
): string {
  const { node } = ctx;
  const lines: string[] = [];

  lines.push(`Symbol: ${node.qualified_name}`);
  lines.push(`Kind: ${node.kind}`);
  lines.push(`Location: ${node.file_path}:${node.start_line}-${node.end_line}`);
  lines.push(`Language: ${node.language}`);
  if (node.signature) lines.push(`Signature: ${node.signature}`);
  if (node.docstring) {
    lines.push('');
    lines.push('Docstring:');
    lines.push(node.docstring);
  }

  const commits = evidence
    .filter((e) => e.source === 'git_commit')
    .sort((a, b) => commitTime(b) - commitTime(a));
  const blames = evidence.filter((e) => e.source === 'git_blame');

  lines.push('');
  lines.push(`Evidence: ${evidence.length} item(s) — ${commits.length} commit(s), ${blames.length} blame entr(ies).`);

  if (commits.length > 0) {
    lines.push('');
    lines.push('Commits (newest first):');
    for (const c of commits) {
      const p = (c.payload ?? {}) as Record<string, unknown>;
      const sha = (c.ref ?? '').slice(0, 8);
      const date = isoDay(p.author_time as number | undefined);
      const author = (p.author as string | undefined) ?? 'unknown';
      const subject = (p.subject as string | undefined) ?? '';
      const body = ((p.body as string | undefined) ?? '').trim();
      lines.push('');
      lines.push(`  COMMIT ${sha}  ${date}  by ${author}`);
      lines.push(`    ${subject}`);
      if (body) {
        for (const bodyLine of body.split('\n')) {
          lines.push(`    ${bodyLine}`);
        }
      }
    }
  }

  if (blames.length > 0) {
    lines.push('');
    lines.push('Blame (line attribution within the symbol\'s range):');
    for (const b of blames) {
      const p = (b.payload ?? {}) as Record<string, unknown>;
      const sha = (b.ref ?? '').slice(0, 8);
      const lineCount = (p.line_count as number | undefined) ?? 0;
      const lineTotal = (p.line_total as number | undefined) ?? 0;
      const summary = (p.summary as string | undefined) ?? '';
      lines.push(`  ${sha}  ${lineCount}/${lineTotal} lines  — ${summary}`);
    }
  }

  return lines.join('\n');
}

function commitTime(r: EvidenceRecord): number {
  const p = r.payload as Record<string, unknown> | null;
  if (!p || typeof p !== 'object') return 0;
  const t = p.author_time;
  return typeof t === 'number' ? t : 0;
}

function isoDay(unixSeconds: number | undefined): string {
  if (!unixSeconds) return '????-??-??';
  return new Date(unixSeconds * 1000).toISOString().slice(0, 10);
}
