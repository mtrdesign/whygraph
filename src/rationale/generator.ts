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

export interface GeneratorOptions {
  apiKey: string;
  model: string;
  effort?: 'low' | 'medium' | 'high' | 'max';
  maxTokens?: number;
}

export interface GenerationResult {
  rationale: Rationale;
  promptVersion: string;
  model: string;
  usage: {
    inputTokens: number;
    cacheReadInputTokens: number;
    cacheCreationInputTokens: number;
    outputTokens: number;
  };
}

export class RationaleGenerator {
  private readonly client: Anthropic;
  private readonly model: string;
  private readonly effort: 'low' | 'medium' | 'high' | 'max';
  private readonly maxTokens: number;

  constructor(opts: GeneratorOptions) {
    this.client = new Anthropic({ apiKey: opts.apiKey });
    this.model = opts.model;
    this.effort = opts.effort ?? 'medium';
    this.maxTokens = opts.maxTokens ?? 2048;
  }

  async generate(
    ctx: SymbolContext,
    evidence: EvidenceRecord[]
  ): Promise<GenerationResult> {
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
        // The SDK helper's signature targets the zod v3 ZodType. Our schema is
        // built with `zod/v4` for runtime compatibility with the SDK's v4
        // JSON-schema generator; cast bridges the v3/v4 type gap.
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
      usage: {
        inputTokens: response.usage.input_tokens,
        cacheReadInputTokens: response.usage.cache_read_input_tokens ?? 0,
        cacheCreationInputTokens:
          response.usage.cache_creation_input_tokens ?? 0,
        outputTokens: response.usage.output_tokens,
      },
    };
  }
}
