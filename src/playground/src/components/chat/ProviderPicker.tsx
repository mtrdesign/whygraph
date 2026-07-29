import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api";
import { Button, Input, Select, Spinner } from "../../lib/ui";

/**
 * Provider + model chooser for a new session.
 *
 * Unconfigured providers stay visible but disabled, labelled with the env var
 * they need — "openrouter (set OPENROUTER_API_KEY)" is actionable in a way that
 * a missing row is not. The model field is free text, pre-filled with the
 * provider's default: the set of valid model ids changes faster than this UI
 * could track, and OpenRouter's is effectively open-ended.
 */
export function ProviderPicker({
  onCreate,
  onCancel,
  creating,
}: {
  onCreate: (provider: string, model: string) => void;
  onCancel: () => void;
  creating: boolean;
}) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["chat", "providers"],
    queryFn: api.chatProviders,
  });

  const [provider, setProvider] = useState<string>("");
  const [model, setModel] = useState<string>("");

  // Default to the first configured provider once the list arrives, so the
  // common case is one click.
  useEffect(() => {
    if (!data || provider) return;
    const first = data.find((p) => p.configured) ?? data[0];
    if (first) {
      setProvider(first.provider);
      setModel(first.default_model);
    }
  }, [data, provider]);

  if (isLoading) return <div className="p-3"><Spinner label="Loading providers…" /></div>;
  if (isError)
    return <div className="p-3 text-xs text-rose-400">{(error as Error).message}</div>;
  if (!data) return null;

  const selected = data.find((p) => p.provider === provider);
  const anyConfigured = data.some((p) => p.configured);

  return (
    <div className="space-y-2 border-b border-border bg-panel2/40 p-3">
      <Select
        value={provider}
        onChange={(e) => {
          const next = e.target.value;
          setProvider(next);
          setModel(data.find((p) => p.provider === next)?.default_model ?? "");
        }}
      >
        {data.map((p) => (
          <option key={p.provider} value={p.provider} disabled={!p.configured}>
            {p.provider}
            {p.configured ? "" : ` — set ${p.env_var}`}
          </option>
        ))}
      </Select>

      <Input
        value={model}
        placeholder="model id"
        onChange={(e) => setModel(e.target.value)}
      />

      {!anyConfigured && (
        <div className="rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-300">
          No chat provider is configured. Set one provider's API key in the
          environment or in <span className="font-mono">whygraph.toml</span>, then
          restart <span className="font-mono">whygraph serve</span>.
        </div>
      )}

      <div className="flex gap-2">
        <Button
          onClick={() => onCreate(provider, model)}
          disabled={!provider || !selected?.configured || creating}
          className="flex-1"
        >
          {creating ? "Creating…" : "Start chat"}
        </Button>
        <Button variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}
