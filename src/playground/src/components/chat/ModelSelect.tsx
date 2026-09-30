import { useMemo, useState } from "react";
import { useProjectQuery } from "../../lib/project";
import { Input } from "../ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select";
import { cn } from "@/lib/utils";

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

  const providers = useProjectQuery(["chat", "providers"], (api) => api.chatProviders());

  const models = useProjectQuery(["chat", "models", provider], (api) => api.chatModels(provider), {
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
  // active, or it's a hand-typed id). Keep it selectable so the select never
  // silently reports a different model than the session is actually using.
  const missingCurrent = !!model && !visible.some((m) => m.id === model);

  const providerItems = (providers.data ?? []).map((p) => ({
    value: p.provider,
    label: p.configured ? p.provider : `${p.provider} — set ${p.env_var}`,
    disabled: !p.configured,
  }));
  const modelItems = [
    ...(models.isLoading && model ? [{ value: model, label: "loading models…" }] : []),
    ...(missingCurrent && !models.isLoading ? [{ value: model, label: model }] : []),
    ...visible.map((m) => ({ value: m.id, label: m.display_name })),
  ];

  const triggerClass = compact ? "w-auto" : "w-full";

  return (
    <div className={cn(compact ? "flex items-center gap-2" : "space-y-2")}>
      <Select
        items={providerItems}
        value={provider || null}
        disabled={disabled || providers.isLoading}
        onValueChange={(next) => {
          if (!next) return;
          setFilter("");
          // Model is left empty: the old id is meaningless on a new provider,
          // and the server resolves that provider's default.
          onChange({ provider: next, model: "" });
        }}
      >
        <SelectTrigger aria-label="Provider" size={compact ? "sm" : "default"} className={triggerClass}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent alignItemWithTrigger={false} className="w-auto">
          {providerItems.map((p) => (
            <SelectItem key={p.value} value={p.value} disabled={p.disabled}>
              {p.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      <div className={cn(compact ? "flex items-center gap-2" : "space-y-2")}>
        {showFilter && (
          <Input
            aria-label="Filter models"
            value={filter}
            placeholder={`Filter ${options.length} models…`}
            className={cn(compact && "h-7 w-40 px-2 text-xs")}
            onChange={(e) => setFilter(e.target.value)}
          />
        )}

        <Select
          items={modelItems}
          value={model || null}
          disabled={disabled || models.isLoading}
          onValueChange={(next) => {
            if (next) onChange({ provider, model: next });
          }}
        >
          <SelectTrigger
            aria-label="Model"
            size={compact ? "sm" : "default"}
            className={cn(triggerClass, compact && "max-w-[16rem]")}
          >
            <SelectValue
              placeholder={
                models.isLoading ? "loading models…" : modelItems.length === 0 ? "no matches" : "select a model"
              }
            />
          </SelectTrigger>
          <SelectContent alignItemWithTrigger={false} className="w-auto max-w-[28rem]">
            {modelItems.length === 0 && (
              <div className="px-2 py-1.5 text-sm text-muted-foreground">no matches</div>
            )}
            {modelItems.map((m) => (
              <SelectItem key={m.value} value={m.value}>
                {m.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {models.data?.source === "fallback" && !compact && (
        <div className="rounded-sm border border-warning/30 bg-warning/10 px-2 py-1 text-[11px] text-warning">
          Couldn't list this provider's models, so only known defaults are shown.
          {models.data.error ? ` (${models.data.error})` : ""}
        </div>
      )}
      {models.data?.source === "fallback" && compact && (
        <span
          title={`Couldn't list models: ${models.data.error ?? "unknown error"}`}
          className="shrink-0 text-[11px] text-warning"
          aria-label="Model list unavailable; showing defaults"
        >
          ⚠
        </span>
      )}
    </div>
  );
}
