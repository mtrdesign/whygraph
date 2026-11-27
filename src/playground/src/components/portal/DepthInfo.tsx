import type { ReactNode } from "react";
import { InfoIcon } from "lucide-react";
import type { DepthProfile } from "../../api";
import {
  DEPTH_COPY,
  DEPTH_LEVELS,
  callsText,
  depthShortName,
  formatCostRange,
  type DepthLevel,
} from "../../lib/rationaleDepth";
import { useProjectQuery } from "../../lib/project";
import { Button } from "../ui/button";
import { Popover, PopoverContent, PopoverHeader, PopoverTitle, PopoverTrigger } from "../ui/popover";
import { Skeleton } from "../ui/skeleton";
import { ResponsiveTable, type Column } from "../layout/ResponsiveTable";
import { ErrorState } from "../state/ErrorState";

type Ready = Extract<DepthProfile, { status: "ready" }>;

interface Row {
  key: "auto" | DepthLevel;
  name: string;
  description: string;
}

const ROWS: Row[] = [
  { key: "auto", name: DEPTH_COPY.auto.name, description: DEPTH_COPY.auto.description },
  ...DEPTH_LEVELS.map((level) => {
    const row = DEPTH_COPY.levels[level];
    return {
      key: level,
      name: row.name,
      description: `${row.question} ${row.reads} ${callsText(row.calls)}`,
    };
  }),
];

/** One level's price cell, or `null` for Auto (it has no price of its own). */
function priceCell(row: Row, profile: DepthProfile | undefined, pending: boolean): ReactNode {
  if (row.key === "auto") return null;
  if (pending) return <Skeleton className="h-4 w-24" />;
  if (profile?.status !== "ready") return null;
  if (profile.cost_hidden) return "Not shown";
  const range = formatCostRange(profile.levels[row.key].cost);
  return range ? `about ${range} (estimate)` : "Price unknown";
}

function Lines({ profile }: { profile: Ready }) {
  const medium = profile.levels.medium;
  const share = Math.round(profile.descriptions.undescribed_share * 100);
  const first = formatCostRange(medium.first_card);
  const each = profile.descriptions.per_commit_usd;
  const cheaper = profile.cheaper_descriptions;
  const priceUnknown = !profile.cost_hidden && !profile.model.price_known;
  return (
    <div className="flex flex-col gap-1 text-[13px] text-muted-foreground">
      {priceUnknown && (
        <p data-testid="depth-price-unknown">
          Price unknown until this project pins a rationale model or generates a card.
        </p>
      )}
      <p>{DEPTH_COPY.footer}</p>
      {each !== null && (
        <p data-testid="depth-description-cost">
          A commit without a description yet costs about {formatCostRange({ low: each, high: each })}, once.
        </p>
      )}
      {first && share > 0 && (
        <p data-testid="depth-first-card">
          The first card on code whose commits are not described yet costs about {first} (in this project, {share}% of
          the sampled commits have no description).
        </p>
      )}
      {cheaper && each !== null && (
        <p data-testid="depth-cheaper">
          With {cheaper.provider}
          {cheaper.model ? `/${cheaper.model}` : ""} a description would cost about{" "}
          {formatCostRange({ low: cheaper.per_commit_usd, high: cheaper.per_commit_usd })} instead of{" "}
          {formatCostRange({ low: each, high: each })}. Nothing changes unless you change the model.
        </p>
      )}
      {profile.model.model && !profile.cost_hidden && profile.model.price_known && (
        <p>
          * {profile.model.model}
          {profile.prices_as_of ? `, prices as of ${profile.prices_as_of}` : ""}. Measured on {profile.sampled} sampled
          symbols; output length is {profile.output_source === "ledger" ? "this project's median" : "assumed"}.
        </p>
      )}
    </div>
  );
}

/** The panel's body: the comparison and, when the estimate is wanted, the prices. */
export function DepthPanel({ withPrices = true }: { withPrices?: boolean }) {
  const { data, isLoading, isError, error, refetch } = useProjectQuery(
    ["depth-profile"],
    (api) => api.depthProfile(),
    {
      enabled: withPrices,
      staleTime: 60_000,
      refetchInterval: (query) => (query.state.data?.status === "computing" ? 1500 : false),
    },
  );
  const pending = withPrices && (isLoading || data?.status === "computing");
  const columns: Column<Row>[] = [
    { key: "name", header: "Level", primary: true, cell: (r) => r.name },
    { key: "description", header: "What it includes", cell: (r) => r.description },
    ...(withPrices
      ? [{ key: "price", header: "Price per card", cell: (r: Row) => priceCell(r, data, pending) } satisfies Column<Row>]
      : []),
  ];
  return (
    <div className="flex flex-col gap-3" data-testid="depth-panel">
      <PopoverHeader>
        <PopoverTitle>{DEPTH_COPY.title}</PopoverTitle>
      </PopoverHeader>
      <ResponsiveTable columns={columns} rows={ROWS} rowKey={(r) => r.key} />
      {withPrices && isError && (
        <ErrorState size="inline" error={error} onRetry={() => void refetch()} />
      )}
      {withPrices && data?.status === "no_index" && (
        <p className="text-[13px] text-muted-foreground" data-testid="depth-no-index">
          The estimate needs a CodeGraph index; run a scan.
        </p>
      )}
      {withPrices && data?.status === "no_history" && (
        <p className="text-[13px] text-muted-foreground" data-testid="depth-no-history">
          The estimate needs scanned history; run a scan.
        </p>
      )}
      {withPrices && data?.status === "ready" && <Lines profile={data} />}
      {(!withPrices || data?.status !== "ready") && !isError && (
        <p className="text-[13px] text-muted-foreground">{DEPTH_COPY.footer}</p>
      )}
    </div>
  );
}

const CONTENT = "w-[40rem] max-w-[calc(100vw-2rem)]";

/**
 * The (i) beside a depth label: a ghost icon button that opens the comparison on
 * hover, click or tap (click pins it), Enter or Space; Escape closes it and gives
 * focus back. Not for a linked project (its cards are the platform's).
 */
export function DepthInfoButton({ withPrices = true }: { withPrices?: boolean }) {
  return (
    <Popover>
      <PopoverTrigger
        openOnHover
        delay={150}
        closeDelay={120}
        render={<Button type="button" variant="ghost" size="icon-xs" aria-label={DEPTH_COPY.title} />}
      >
        <InfoIcon />
      </PopoverTrigger>
      <PopoverContent align="start" className={CONTENT}>
        <DepthPanel withPrices={withPrices} />
      </PopoverContent>
    </Popover>
  );
}

/** A card's depth badge ("Medium - Context"): the same panel, opened by the badge. */
export function DepthBadge({ level }: { level: string }) {
  const name = level in DEPTH_COPY.levels ? DEPTH_COPY.levels[level as DepthLevel].name : depthShortName(level);
  return (
    <Popover>
      <PopoverTrigger
        openOnHover
        delay={150}
        closeDelay={120}
        render={<Button type="button" variant="outline" size="xs" data-testid="depth-badge" />}
      >
        {name}
        <InfoIcon data-icon="inline-end" />
      </PopoverTrigger>
      <PopoverContent align="end" className={CONTENT}>
        <DepthPanel />
      </PopoverContent>
    </Popover>
  );
}
