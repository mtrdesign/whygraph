import { spawnSync } from 'node:child_process';
import Anthropic from '@anthropic-ai/sdk';
import { zodOutputFormat } from '@anthropic-ai/sdk/helpers/zod';
import type { ZodType } from 'zod';
import type { EvidenceRecord } from '../evidence/store.js';
import {
  PROMPT_VERSION,
  RationaleSchema,
  SYSTEM_PROMPT,
  buildUserPrompt,
  type Rationale,
  type SymbolContext,
} from './prompt.js';

export type RationaleBackend = 'api' | 'claude_cli';

export interface GeneratorOptions {
  backend: RationaleBackend;
  model: string;
  apiKey?: string;                                    // required when backend === 'api'
  effort?: 'low' | 'medium' | 'high' | 'max';
  maxTokens?: number;
}

export interface GenerationResult {
  rationale: Rationale;
  promptVersion: string;
  model: string;
  backend: RationaleBackend;
  usage: {
    inputTokens: number;
    cacheReadInputTokens: number;
    cacheCreationInputTokens: number;
    outputTokens: number;
  };
}

const ZERO_USAGE = {
  inputTokens: 0,
  cacheReadInputTokens: 0,
  cacheCreationInputTokens: 0,
  outputTokens: 0,
};

export class RationaleGenerator {
  private readonly backend: RationaleBackend;
  private readonly model: string;
  private readonly effort: 'low' | 'medium' | 'high' | 'max';
  private readonly maxTokens: number;
  private readonly client: Anthropic | null;

  constructor(opts: GeneratorOptions) {
    this.backend = opts.backend;
    this.model = opts.model;
    this.effort = opts.effort ?? 'medium';
    this.maxTokens = opts.maxTokens ?? 2048;
    if (this.backend === 'api') {
      if (!opts.apiKey) {
        throw new Error('RationaleGenerator(api): apiKey is required');
      }
      this.client = new Anthropic({ apiKey: opts.apiKey });
    } else {
      this.client = null;
    }
  }

  async generate(
    ctx: SymbolContext,
    evidence: EvidenceRecord[]
  ): Promise<GenerationResult> {
    return this.backend === 'claude_cli'
      ? this.generateViaCli(ctx, evidence)
      : this.generateViaApi(ctx, evidence);
  }

  private async generateViaApi(
    ctx: SymbolContext,
    evidence: EvidenceRecord[]
  ): Promise<GenerationResult> {
    if (!this.client) throw new Error('Anthropic client not initialised');
    const userPrompt = buildUserPrompt(ctx, evidence);

    const response = await this.client.messages.parse({
      model: this.model,
      max_tokens: this.maxTokens,
      thinking: { type: 'disabled' },
      system: [
        {
          type: 'text',
          text: SYSTEM_PROMPT,
          cache_control: { type: 'ephemeral' },
        },
      ],
      output_config: {
        effort: this.effort,
        format: zodOutputFormat(
          RationaleSchema as unknown as ZodType<Rationale>
        ),
      },
      messages: [{ role: 'user', content: userPrompt }],
    });

    if (response.stop_reason === 'refusal') {
      throw new Error(
        `Claude refused to generate rationale for ${ctx.node.qualified_name}`
      );
    }
    if (!response.parsed_output) {
      throw new Error(
        `Claude response did not match the rationale schema (stop_reason=${response.stop_reason})`
      );
    }

    return {
      rationale: response.parsed_output,
      promptVersion: PROMPT_VERSION,
      model: this.model,
      backend: 'api',
      usage: {
        inputTokens: response.usage.input_tokens,
        cacheReadInputTokens: response.usage.cache_read_input_tokens ?? 0,
        cacheCreationInputTokens:
          response.usage.cache_creation_input_tokens ?? 0,
        outputTokens: response.usage.output_tokens,
      },
    };
  }

  private async generateViaCli(
    ctx: SymbolContext,
    evidence: EvidenceRecord[]
  ): Promise<GenerationResult> {
    const userPrompt = buildUserPrompt(ctx, evidence);
    const args = [
      '-p',
      '--system-prompt',
      SYSTEM_PROMPT,
      '--output-format',
      'json',
      '--model',
      this.model,
    ];

    // Strip ANTHROPIC_API_KEY from the child's env: when set, the claude CLI
    // routes via that key (and would hit any direct-API billing limit on it)
    // instead of the user's Claude Code subscription OAuth. Removing it lets
    // claude fall back to the subscription path, which is the whole point of
    // the claude_cli backend.
    const childEnv = { ...process.env };
    delete childEnv.ANTHROPIC_API_KEY;

    const child = spawnSync('claude', args, {
      input: userPrompt,
      encoding: 'utf8',
      maxBuffer: 32 * 1024 * 1024,
      env: childEnv,
    });

    if (child.error) {
      throw new Error(`claude CLI failed to spawn: ${child.error.message}`);
    }
    if (child.status !== 0) {
      const stderr = (child.stderr ?? '').trim();
      throw new Error(
        `claude CLI exited ${child.status}${stderr ? `: ${stderr}` : ''}`
      );
    }
    const stdout = child.stdout;

    let envelope: Record<string, unknown>;
    try {
      envelope = JSON.parse(stdout);
    } catch {
      throw new Error(
        `claude CLI did not return JSON envelope (got ${stdout.slice(0, 120)}...)`
      );
    }

    const text = extractText(envelope);
    const json = extractJson(text);
    const validated = RationaleSchema.parse(json) as Rationale;

    return {
      rationale: validated,
      promptVersion: PROMPT_VERSION,
      model: this.model,
      backend: 'claude_cli',
      usage: extractUsage(envelope),
    };
  }
}

function extractText(env: Record<string, unknown>): string {
  // Claude CLI's --output-format json envelope shape (as of 2.1):
  //   { type: "result", subtype: "success", result: "...", usage: {...}, ... }
  // Fall back to a few alternative shapes just in case.
  if (typeof env.result === 'string') return env.result;
  const content = env.content;
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    for (const block of content) {
      if (
        block &&
        typeof block === 'object' &&
        (block as { type?: string }).type === 'text' &&
        typeof (block as { text?: string }).text === 'string'
      ) {
        return (block as { text: string }).text;
      }
    }
  }
  if (typeof (env as { text?: string }).text === 'string') {
    return (env as { text: string }).text;
  }
  throw new Error('claude CLI envelope did not contain a text result');
}

function extractJson(text: string): unknown {
  const trimmed = text.trim();

  // 1. Try parsing the whole thing.
  try {
    return JSON.parse(trimmed);
  } catch {
    /* fall through */
  }

  // 2. Strip a leading ```json (or ```) fence and a trailing ``` if present.
  const stripped = trimmed
    .replace(/^```(?:json|JSON)?\s*\r?\n/, '')
    .replace(/\r?\n```\s*$/, '');
  if (stripped !== trimmed) {
    try {
      return JSON.parse(stripped.trim());
    } catch {
      /* fall through */
    }
  }

  // 3. Last resort: take the substring from first { to last }.
  const first = trimmed.indexOf('{');
  const last = trimmed.lastIndexOf('}');
  if (first !== -1 && last > first) {
    try {
      return JSON.parse(trimmed.slice(first, last + 1));
    } catch {
      /* fall through */
    }
  }

  throw new Error(
    `could not parse JSON from claude output (first 200 chars: ${trimmed.slice(0, 200)})`
  );
}

function extractUsage(
  env: Record<string, unknown>
): GenerationResult['usage'] {
  const usage = env.usage;
  if (!usage || typeof usage !== 'object') return ZERO_USAGE;
  const u = usage as Record<string, unknown>;
  const num = (v: unknown): number => (typeof v === 'number' ? v : 0);
  return {
    inputTokens: num(u.input_tokens),
    cacheReadInputTokens: num(u.cache_read_input_tokens),
    cacheCreationInputTokens: num(u.cache_creation_input_tokens),
    outputTokens: num(u.output_tokens),
  };
}
