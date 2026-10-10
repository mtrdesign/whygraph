import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { portalKey, pricesApi, type PriceBody, type PriceRow } from "../../api";
import { authMessage } from "../../lib/authErrors";
import { formatDateTime, formatUsd } from "../../lib/format";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Skeleton } from "../ui/skeleton";
import { Field, nativeSelectClass } from "../portal/Field";
import { ResponsiveTable, type Column } from "../layout/ResponsiveTable";
import { UsageError, UsageSection } from "./parts";

export const PRICES_KEY = portalKey("prices");

/** The providers a price may name (`LlmClientFactory.BUILTIN_PROVIDERS`). */
export const PRICE_PROVIDERS = ["anthropic", "openai", "openrouter", "deepseek", "ollama"] as const;

const MAX_RATE = 10_000;

type RateField = "input_per_mtok" | "output_per_mtok" | "cache_read_per_mtok" | "cache_write_per_mtok";
const RATE_FIELDS: { key: RateField; label: string; required: boolean }[] = [
  { key: "input_per_mtok", label: "Input", required: true },
  { key: "output_per_mtok", label: "Output", required: true },
  { key: "cache_read_per_mtok", label: "Cache read", required: false },
  { key: "cache_write_per_mtok", label: "Cache write", required: false },
];

type Draft = Record<RateField, string>;

// A price per million tokens: the money format for whole cents, more places for a fraction of one.
const rate = (v: number | null) =>
  v === null
    ? "-"
    : Math.round(v * 100) / 100 === v
      ? formatUsd(v)
      : `$${v.toLocaleString("en-US", { maximumFractionDigits: 6 })}`;

function draftOf(row?: PriceRow): Draft {
  const s = (v: number | null | undefined) => (v === null || v === undefined ? "" : String(v));
  return {
    input_per_mtok: s(row?.input_per_mtok),
    output_per_mtok: s(row?.output_per_mtok),
    cache_read_per_mtok: s(row?.cache_read_per_mtok),
    cache_write_per_mtok: s(row?.cache_write_per_mtok),
  };
}

/** The rates of a draft, or the first field's problem. */
export function parseRates(draft: Draft): { rates: Omit<PriceBody, "provider" | "model"> } | { error: string; field: RateField } {
  const out: Partial<Record<RateField, number | null>> = {};
  for (const { key, label, required } of RATE_FIELDS) {
    const text = draft[key].trim().replace(/^\$/, "");
    if (text === "") {
      if (required) return { error: `${label} price is required.`, field: key };
      out[key] = null;
      continue;
    }
    const n = Number(text);
    if (!/^\d+(\.\d+)?$/.test(text) || !Number.isFinite(n) || n < 0 || n > MAX_RATE) {
      return { error: `${label} price must be from $0 to $10,000 per million tokens.`, field: key };
    }
    out[key] = n;
  }
  return {
    rates: {
      input_per_mtok: out.input_per_mtok as number,
      output_per_mtok: out.output_per_mtok as number,
      cache_read_per_mtok: out.cache_read_per_mtok ?? null,
      cache_write_per_mtok: out.cache_write_per_mtok ?? null,
    },
  };
}

function RateInputs({ draft, onChange }: { draft: Draft; onChange: (d: Draft) => void }) {
  return (
    <>
      {RATE_FIELDS.map(({ key, label, required }) => (
        <Field key={key} label={label} className="w-28">
          {(p) => (
            <Input
              {...p}
              inputMode="decimal"
              placeholder={required ? "e.g. 3.00" : "optional"}
              value={draft[key]}
              onChange={(e) => onChange({ ...draft, [key]: e.target.value })}
            />
          )}
        </Field>
      ))}
    </>
  );
}

function useSavePrice(onDone: () => void, setError: (e: string | null) => void) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: PriceBody) => pricesApi.put(body),
    onSuccess: async (row) => {
      setError(null);
      toast.success(`Price of ${row.model} saved`);
      onDone();
      await queryClient.invalidateQueries({ queryKey: PRICES_KEY });
    },
    onError: (err) => setError(authMessage(err)),
  });
}

/** One row's override form, opened in place. */
function OverrideForm({ row, onClose }: { row: PriceRow; onClose: () => void }) {
  const [draft, setDraft] = useState(() => draftOf(row));
  const [error, setError] = useState<string | null>(null);
  const save = useSavePrice(onClose, setError);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const parsed = parseRates(draft);
    if ("error" in parsed) return setError(parsed.error);
    save.mutate({ provider: row.provider, model: row.model, ...parsed.rates });
  };
  return (
    <form
      onSubmit={submit}
      className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3 shadow-card"
      data-testid={`price-form-${row.provider}-${row.model}`}
      noValidate
    >
      <p className="text-xs font-medium">
        Override the price of <span className="font-mono">{row.model}</span> ({row.provider})
      </p>
      <div className="flex flex-wrap items-end gap-3">
        <RateInputs draft={draft} onChange={setDraft} />
        <Button type="submit" size="sm" disabled={save.isPending}>
          Save override
        </Button>
        <Button type="button" size="sm" variant="outline" onClick={onClose}>
          Cancel
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}
    </form>
  );
}

/** A price for a model the table does not list (a DeepSeek model, a custom endpoint's). */
function AddPrice() {
  const [provider, setProvider] = useState<string>("openai");
  const [model, setModel] = useState("");
  const [draft, setDraft] = useState(() => draftOf());
  const [error, setError] = useState<string | null>(null);
  const save = useSavePrice(() => {
    setModel("");
    setDraft(draftOf());
  }, setError);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!model.trim()) return setError("Enter the model id as the provider names it.");
    const parsed = parseRates(draft);
    if ("error" in parsed) return setError(parsed.error);
    save.mutate({ provider, model: model.trim(), ...parsed.rates });
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-2" data-testid="add-price" noValidate>
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Provider" className="w-36">
          {(p) => (
            <select {...p} className={nativeSelectClass} value={provider} onChange={(e) => setProvider(e.target.value)}>
              {PRICE_PROVIDERS.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          )}
        </Field>
        <Field label="Model" className="w-56">
          {(p) => <Input {...p} placeholder="e.g. deepseek-chat" value={model} onChange={(e) => setModel(e.target.value)} />}
        </Field>
        <RateInputs draft={draft} onChange={setDraft} />
        <Button type="submit" size="sm" disabled={save.isPending}>
          Add price
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}
    </form>
  );
}

/**
 * The Prices tab: the bundled table with the org's overrides on top. Owners and
 * org admins (`org.budgets`) override a row, revert it, or price a model the table
 * lacks; everyone else reads. A new price applies to new calls only.
 */
export function PricesTab({ canEdit }: { canEdit: boolean }) {
  const queryClient = useQueryClient();
  const prices = useQuery({ queryKey: PRICES_KEY, queryFn: pricesApi.get });
  const [editing, setEditing] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [revertError, setRevertError] = useState<string | null>(null);
  const revert = useMutation({
    mutationFn: (row: PriceRow) => pricesApi.revert(row.provider, row.model),
    onSuccess: async (_, row) => {
      setRevertError(null);
      toast.success(`Override of ${row.model} removed`);
      await queryClient.invalidateQueries({ queryKey: PRICES_KEY });
    },
    onError: (err) => setRevertError(authMessage(err)),
  });

  if (prices.isLoading) return <Skeleton className="h-40" />;
  if (prices.isError || !prices.data) {
    return (
      <UsageError
        error={prices.error}
        what="the price table"
        title="Couldn't load the prices"
        onRetry={() => void prices.refetch()}
      />
    );
  }
  const needle = filter.trim().toLowerCase();
  const rows = prices.data.rows.filter(
    (r) => !needle || r.model.toLowerCase().includes(needle) || r.provider.toLowerCase().includes(needle),
  );
  const keyOf = (r: PriceRow) => `${r.provider}/${r.model}`;
  const editingRow = prices.data.rows.find((r) => keyOf(r) === editing);
  const right = (v: number | null) => <span className="whitespace-nowrap">{rate(v)}</span>;
  const columns: Column<PriceRow>[] = [
    { key: "provider", header: "Provider", cell: (r) => <span className="text-muted-foreground">{r.provider}</span> },
    {
      key: "model",
      header: "Model",
      primary: true,
      cell: (r) => (
        <span className="font-mono text-xs" title={r.model}>
          {r.model}
        </span>
      ),
    },
    { key: "in", header: "Input", align: "right", cell: (r) => right(r.input_per_mtok) },
    { key: "out", header: "Output", align: "right", cell: (r) => right(r.output_per_mtok) },
    { key: "cr", header: "Cache read", align: "right", cell: (r) => right(r.cache_read_per_mtok) },
    { key: "cw", header: "Cache write", align: "right", cell: (r) => right(r.cache_write_per_mtok) },
    {
      key: "source",
      header: "Source",
      cell: (r) =>
        r.origin === "override" ? (
          <Badge variant="secondary" title={r.updated_at ? `Set ${formatDateTime(r.updated_at)}` : undefined}>
            override
          </Badge>
        ) : (
          <Badge variant="outline">bundled</Badge>
        ),
    },
  ];
  if (canEdit) {
    columns.push({
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      cell: (r) => (
        <span className="flex flex-wrap justify-end gap-1.5">
          <Button type="button" size="xs" variant="outline" onClick={() => setEditing(keyOf(r))} disabled={editing === keyOf(r)}>
            Override
          </Button>
          {r.origin === "override" && (
            <Button
              type="button"
              size="xs"
              variant="outline"
              disabled={revert.isPending}
              onClick={() => revert.mutate(r)}
              title="Remove the override: the bundled price applies again, if the table has one"
            >
              Revert
            </Button>
          )}
        </span>
      ),
    });
  }

  return (
    <div className="flex flex-col gap-6" data-testid="prices-tab">
      <p className="text-[13px] text-muted-foreground">
        USD per million tokens. The bundled table is dated{" "}
        <span className="font-medium text-foreground" data-testid="prices-as-of">
          {prices.data.as_of}
        </span>{" "}
        and ships with each release. An override applies to new calls at once; recorded calls keep the cost they were
        written with. A provider with a custom endpoint is priced by your overrides only.
        {!canEdit && " Owners and org admins change prices."}
      </p>
      {canEdit && (
        <UsageSection title="Add a price" description="For a model the table does not list.">
          <AddPrice />
        </UsageSection>
      )}
      <UsageSection
        title="Price table"
        actions={
          <Input
            aria-label="Filter models"
            placeholder="Filter models"
            className="w-48 max-sm:w-full"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        }
      >
        {revertError && (
          <p role="alert" className="text-xs text-destructive">
            {revertError}
          </p>
        )}
        {editingRow && <OverrideForm key={keyOf(editingRow)} row={editingRow} onClose={() => setEditing(null)} />}
        <div data-testid="price-table">
          <ResponsiveTable
            columns={columns}
            rows={rows}
            rowKey={keyOf}
            rowTestId={(r) => `price-${r.provider}-${r.model}`}
            empty={<p className="py-2 text-sm text-muted-foreground">No model matches.</p>}
          />
        </div>
      </UsageSection>
    </div>
  );
}
