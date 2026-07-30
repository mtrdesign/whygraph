import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { clsx } from "clsx";
import { api } from "../../api";
import { Input, Select } from "../../lib/ui";

// Provider + model, as two dropdowns. Used in both places a model gets chosen:
// the New-chat panel and the thread header.
//
// Model options are fetched live from the provider rather than hardcoded — a
// baked-in list rots on every model release and could never cover OpenRouter's
// several-hundred-model catalogue. Two consequences shape this component:
//
//  - Listing can fail on a key that chats perfectly well (Anthropic's /models
//    rejects scoped keys that /messages accepts). The backend then returns its
//    short built-in list with source: "fallback", and we say so rather than
//    silently showing four options as if they were all that exist.
//  - OpenRouter returns ~370 entries, so a filter box appears once the list is
//    long enough that scrolling it would be the wrong interaction.

const FILTER_THRESHOLD = 25;

export function ModelSelect({
  provider,
  model,
  onChange,
  disabled,
  compact,
}: {
  provider: string;
  model: string;
  onChange: (next: { provider: string; model: string }) => void;
  disabled?: boolean;
  /** Header variant: smaller controls, laid out in a row. */
  compact?: boolean;
}) {
  const [filter, setFilter] = useState("");

  const providers = useQuery({
    queryKey: ["chat", "providers"],
    queryFn: api.chatProviders,
  });

  const models = useQuery({
    queryKey: ["chat", "models", provider],
    queryFn: () => api.chatModels(provider),
    enabled: !!provider,
    // Model catalogues barely move within a session, and OpenRouter's is a
    // ~370-entry payload — no need to refetch on every mount.
    staleTime: 10 * 60 * 1000,
  });

  const options = models.data?.models ?? [];
  const showFilter = options.length > FILTER_THRESHOLD;
  const visible = useMemo(() => {
    if (!filter.trim()) return options;
    const needle = filter.toLowerCase();
    return options.filter(
      (m) =>
        m.id.toLowerCase().includes(needle) ||
        m.display_name.toLowerCase().includes(needle),
    );
  }, [options, filter]);

  // The session's current model may not be in the fetched list (a filter is
  // active, or it's a hand-typed id). Keep it selectable so the <select> never
  // silently reports a different model than the session is actually using.
  const missingCurrent = model && !visible.some((m) => m.id === model);

  const fieldClass = compact ? "px-2 py-1 text-xs" : "";

  return (
    <div className={clsx(compact ? "flex items-center gap-2" : "space-y-2")}>
      <Select
        aria-label="Provider"
        value={provider}
        disabled={disabled || providers.isLoading}
        className={clsx(fieldClass, compact && "w-auto")}
        onChange={(e) => {
          setFilter("");
          // Model is left empty: the old id is meaningless on a new provider,
          // and the server resolves that provider's default.
          onChange({ provider: e.target.value, model: "" });
        }}
      >
        {(providers.data ?? []).map((p) => (
          <option key={p.provider} value={p.provider} disabled={!p.configured}>
            {p.provider}
            {p.configured ? "" : ` — set ${p.env_var}`}
          </option>
        ))}
      </Select>

      <div className={clsx(compact ? "flex items-center gap-2" : "space-y-2")}>
        {showFilter && (
          <Input
            aria-label="Filter models"
            value={filter}
            placeholder={`Filter ${options.length} models…`}
            className={clsx(fieldClass, compact && "w-40")}
            onChange={(e) => setFilter(e.target.value)}
          />
        )}

        <Select
          aria-label="Model"
          value={model}
          disabled={disabled || models.isLoading}
          className={clsx(fieldClass, compact && "w-auto max-w-[16rem]")}
          onChange={(e) => onChange({ provider, model: e.target.value })}
        >
          {models.isLoading && <option value={model}>loading models…</option>}
          {missingCurrent && <option value={model}>{model}</option>}
          {visible.map((m) => (
            <option key={m.id} value={m.id}>
              {m.display_name}
            </option>
          ))}
          {!models.isLoading && visible.length === 0 && !missingCurrent && (
            <option value="">no matches</option>
          )}
        </Select>
      </div>

      {models.data?.source === "fallback" && !compact && (
        <div className="rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-300">
          Couldn't list this provider's models, so only known defaults are shown.
          {models.data.error ? ` (${models.data.error})` : ""}
        </div>
      )}
      {models.data?.source === "fallback" && compact && (
        <span
          title={`Couldn't list models: ${models.data.error ?? "unknown error"}`}
          className="shrink-0 text-[11px] text-amber-400"
          aria-label="Model list unavailable; showing defaults"
        >
          ⚠
        </span>
      )}
    </div>
  );
}
