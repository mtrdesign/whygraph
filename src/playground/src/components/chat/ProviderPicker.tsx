import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api";
import { Button, Spinner } from "../../lib/ui";
import { ModelSelect } from "./ModelSelect";

/**
 * The New-chat panel: pick a provider and model, then start.
 *
 * The dropdowns themselves live in ModelSelect, shared with the thread header
 * so both places behave identically. This component only owns the draft
 * selection and the Start/Cancel actions.
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

  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");

  // Default to the first configured provider once the list arrives, so the
  // common case is one click. Model is left empty — the server resolves the
  // provider's default if the user doesn't pick one.
  useEffect(() => {
    if (!data || provider) return;
    const first = data.find((p) => p.configured) ?? data[0];
    if (first) setProvider(first.provider);
  }, [data, provider]);

  if (isLoading)
    return (
      <div className="p-3">
        <Spinner label="Loading providers…" />
      </div>
    );
  if (isError)
    return <div className="p-3 text-xs text-rose-400">{(error as Error).message}</div>;
  if (!data) return null;

  const selected = data.find((p) => p.provider === provider);
  const anyConfigured = data.some((p) => p.configured);

  return (
    <div className="space-y-2 border-b border-border bg-panel2/40 p-3">
      <ModelSelect
        provider={provider}
        model={model}
        disabled={creating}
        onChange={(next) => {
          setProvider(next.provider);
          setModel(next.model);
        }}
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
