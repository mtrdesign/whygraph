import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { portalKey, pricesApi, type PriceBody, type PriceRow } from "../../api";
import { authMessage } from "../../lib/authErrors";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Skeleton } from "../ui/skeleton";
import { Field, nativeSelectClass } from "../portal/Field";
import { UsageSection } from "./parts";

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

const rate = (v: number | null) => (v === null ? "-" : `$${v.toLocaleString("en-US", { maximumFractionDigits: 6 })}`);

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
              placeholder={required ? "3.00" : "optional"}
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
    <form onSubmit={submit} className="flex flex-col gap-2 py-2" data-testid={`price-form-${row.provider}-${row.model}`} noValidate>
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
          {(p) => <Input {...p} placeholder="deepseek-chat" value={model} onChange={(e) => setModel(e.target.value)} />}
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
    return <p className="text-sm text-destructive">Failed to load: {authMessage(prices.error)}</p>;
  }
  const needle = filter.trim().toLowerCase();
  const rows = prices.data.rows.filter(
    (r) => !needle || r.model.toLowerCase().includes(needle) || r.provider.toLowerCase().includes(needle),
  );
  const keyOf = (r: PriceRow) => `${r.provider}/${r.model}`;
  const columns = canEdit ? 8 : 7;

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
            className="w-48"
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
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs" data-testid="price-table">
            <thead className="text-muted-foreground">
              <tr>
                <th className="py-1.5 pr-3 font-medium">Provider</th>
                <th className="py-1.5 pr-3 font-medium">Model</th>
                <th className="py-1.5 pr-3 text-right font-medium">Input</th>
                <th className="py-1.5 pr-3 text-right font-medium">Output</th>
                <th className="py-1.5 pr-3 text-right font-medium">Cache read</th>
                <th className="py-1.5 pr-3 text-right font-medium">Cache write</th>
                <th className="py-1.5 pr-3 font-medium">Source</th>
                {canEdit && <th className="py-1.5 font-medium" />}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {rows.map((r) => (
                <PriceTableRow
                  key={keyOf(r)}
                  row={r}
                  canEdit={canEdit}
                  columns={columns}
                  editing={editing === keyOf(r)}
                  onEdit={() => setEditing(keyOf(r))}
                  onClose={() => setEditing(null)}
                  onRevert={() => revert.mutate(r)}
                  reverting={revert.isPending}
                />
              ))}
            </tbody>
          </table>
          {rows.length === 0 && <p className="py-2 text-sm text-muted-foreground">No model matches.</p>}
        </div>
      </UsageSection>
    </div>
  );
}

function PriceTableRow({
  row,
  canEdit,
  columns,
  editing,
  onEdit,
  onClose,
  onRevert,
  reverting,
}: {
  row: PriceRow;
  canEdit: boolean;
  columns: number;
  editing: boolean;
  onEdit: () => void;
  onClose: () => void;
  onRevert: () => void;
  reverting: boolean;
}) {
  const override = row.origin === "override";
  return (
    <>
      <tr data-testid={`price-${row.provider}-${row.model}`} data-origin={row.origin}>
        <td className="py-1.5 pr-3 text-muted-foreground">{row.provider}</td>
        <td className="max-w-[18rem] truncate py-1.5 pr-3 font-mono" title={row.model}>
          {row.model}
        </td>
        <td className="py-1.5 pr-3 text-right tabular-nums">{rate(row.input_per_mtok)}</td>
        <td className="py-1.5 pr-3 text-right tabular-nums">{rate(row.output_per_mtok)}</td>
        <td className="py-1.5 pr-3 text-right tabular-nums">{rate(row.cache_read_per_mtok)}</td>
        <td className="py-1.5 pr-3 text-right tabular-nums">{rate(row.cache_write_per_mtok)}</td>
        <td className="whitespace-nowrap py-1.5 pr-3">
          {override ? (
            <Badge variant="secondary" title={row.updated_at ? `Set ${new Date(row.updated_at).toLocaleString()}` : undefined}>
              override
            </Badge>
          ) : (
            <span className="text-muted-foreground">bundled</span>
          )}
        </td>
        {canEdit && (
          <td className="whitespace-nowrap py-1.5 text-right">
            <Button type="button" size="xs" variant="ghost" onClick={onEdit} disabled={editing}>
              Override
            </Button>
            {override && (
              <Button
                type="button"
                size="xs"
                variant="ghost"
                disabled={reverting}
                onClick={onRevert}
                title="Remove the override: the bundled price applies again, if the table has one"
              >
                Revert
              </Button>
            )}
          </td>
        )}
      </tr>
      {editing && (
        <tr>
          <td colSpan={columns}>
            <OverrideForm row={row} onClose={onClose} />
          </td>
        </tr>
      )}
    </>
  );
}
