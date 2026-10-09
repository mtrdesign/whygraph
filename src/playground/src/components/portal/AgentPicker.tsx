import { useId } from "react";
import { cn } from "@/lib/utils";
import type { DetectedAgent } from "../../api";
import { AGENTS } from "../../lib/agents";
import { Badge } from "../ui/badge";
import { Checkbox } from "../ui/checkbox";

/**
 * Multi-select of the agents to wire up (Claude Code / Cursor / VS Code / Codex).
 * `detected` marks the agents whose config already holds a `whygraph` entry (the
 * wizard pre-selects those); `removed` are the ones the user chose to strip
 * instead, which cannot also be selected.
 */
export function AgentPicker({
  value,
  onChange,
  detected = [],
  removed = [],
}: {
  value: readonly string[];
  onChange: (next: string[]) => void;
  detected?: readonly DetectedAgent[];
  removed?: readonly string[];
}) {
  const groupId = useId();
  const toggle = (id: string, on: boolean) =>
    onChange(on ? [...value.filter((v) => v !== id), id] : value.filter((v) => v !== id));

  return (
    <div role="group" aria-label="Agents" className="grid gap-2 sm:grid-cols-2">
      {AGENTS.map((agent) => {
        const found = detected.filter((d) => d.agent === agent.id);
        const checked = value.includes(agent.id);
        const stripped = removed.includes(agent.id);
        const inputId = `${groupId}-${agent.id}`;
        return (
          <label
            key={agent.id}
            htmlFor={inputId}
            className={cn(
              "flex cursor-pointer items-start gap-3 rounded-lg border border-border p-3 transition-colors hover:bg-muted/40",
              checked && "border-primary/60 bg-primary-soft text-primary-text",
              stripped && "cursor-not-allowed opacity-60",
            )}
          >
            <Checkbox
              id={inputId}
              checked={checked}
              disabled={stripped}
              onCheckedChange={(on) => toggle(agent.id, on)}
              className="mt-0.5"
            />
            <span className="flex min-w-0 flex-col gap-1">
              <span className="flex flex-wrap items-center gap-1.5 text-sm font-medium">
                {agent.label}
                {found.length > 0 && <Badge variant="secondary">detected</Badge>}
                {stripped && <Badge variant="outline">entry will be removed</Badge>}
              </span>
              <span className="font-mono text-xs text-muted-foreground">{agent.file}</span>
              <span className="text-xs text-muted-foreground">{agent.commit}</span>
            </span>
          </label>
        );
      })}
    </div>
  );
}
