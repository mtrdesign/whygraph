import { useEffect, useRef, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { ClipboardListIcon } from "lucide-react";
import { projectApi, projectKey, type PlanRecord, type PlanRef, type PlanStatus } from "../api";
import { Markdown } from "../components/chat/Markdown";
import { PageContainer } from "../components/layout/PageContainer";
import { ClearFilters } from "../components/portal/ClearFilters";
import { EmptyState } from "../components/state/EmptyState";
import { ErrorState } from "../components/state/ErrorState";
import { TableSkeleton } from "../components/state/Skeletons";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Tabs, TabsList, TabsTrigger } from "../components/ui/tabs";
import { planAnchor } from "../lib/planScroll";
import { useProjectQuery, useSlug } from "../lib/project";
import { PLAN_STATUSES, type PlansSearch } from "../lib/routeSearch";

const PAGE = 50;

const STATUS_LABEL: Record<PlanStatus, string> = {
  shipped: "Shipped",
  in_flight: "In flight",
  not_implemented: "Not implemented",
};

const STATUS_VARIANT: Record<PlanStatus, "success" | "info" | "outline"> = {
  shipped: "success",
  in_flight: "info",
  not_implemented: "outline",
};

/** `value`, after it stopped changing for `ms` (the search waits for the typing to pause). */
function useDebounced(value: string, ms: number): string {
  const [out, setOut] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setOut(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return out;
}

export function PlanStatusBadge({ status }: { status: PlanStatus }) {
  return <Badge variant={STATUS_VARIANT[status]}>{STATUS_LABEL[status]}</Badge>;
}

function PlanDetail({
  plan,
  section,
  onClose,
}: {
  plan: PlanRecord;
  section: number | undefined;
  onClose: () => void;
}) {
  const bodyRef = useRef<HTMLDivElement>(null);

  // Open at a part (R19): scroll to it and highlight it briefly.
  useEffect(() => {
    if (section === undefined || !bodyRef.current) return;
    const el = planAnchor(bodyRef.current, plan.outline, section);
    if (!el) return;
    el.scrollIntoView?.({ block: "start" });
    el.classList.add("bg-primary-soft", "rounded-sm");
    const t = setTimeout(() => el.classList.remove("bg-primary-soft", "rounded-sm"), 2500);
    return () => clearTimeout(t);
  }, [plan, section]);

  return (
    <section className="flex flex-col gap-3 rounded-lg border border-border bg-card p-4" data-testid="plan-detail" aria-label="Plan">
      <div className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-sm font-semibold">{plan.title}</h2>
          <PlanStatusBadge status={plan.status} />
          {!plan.present && <span className="text-[13px] text-muted-foreground">No longer in the repository</span>}
        </div>
        <p className="font-mono text-[13px] text-muted-foreground">{plan.path}</p>
      </div>

      {plan.digest && (
        <details className="text-sm" data-testid="plan-digest">
          <summary className="cursor-pointer select-none text-[13px] text-muted-foreground hover:text-foreground">
            Decisions
          </summary>
          <Markdown>{plan.digest}</Markdown>
        </details>
      )}

      <div ref={bodyRef} data-testid="plan-body">
        <Markdown>{plan.body}</Markdown>
      </div>
      {plan.truncated && <p className="text-[13px] text-muted-foreground">Truncated at 512 KiB</p>}
      {plan.redacted_lines > 0 && (
        <p className="text-[13px] text-muted-foreground">
          {plan.redacted_lines} {plan.redacted_lines === 1 ? "line was" : "lines were"} removed by secret redaction
        </p>
      )}

      {plan.implementing_commits.length > 0 && (
        <div className="flex flex-col gap-1" data-testid="plan-commits">
          <h3 className="text-[13px] font-semibold">Commits</h3>
          <ul className="flex flex-col gap-1 text-sm">
            {plan.implementing_commits.map((c) => (
              <li key={c.sha}>
                <span className="font-mono text-[13px]">{c.sha}</span> {c.subject}{" "}
                <span className="text-[13px] text-muted-foreground">
                  {c.on_default_branch ? "on the default branch" : "not on the default branch"}
                </span>
              </li>
            ))}
          </ul>
          {plan.implementing_commits_total > plan.implementing_commits.length && (
            <p className="text-[13px] text-muted-foreground">
              and {plan.implementing_commits_total - plan.implementing_commits.length} more
            </p>
          )}
        </div>
      )}
      {plan.pull_requests.length > 0 && (
        <div className="flex flex-col gap-1" data-testid="plan-prs">
          <h3 className="text-[13px] font-semibold">Pull requests</h3>
          <ul className="flex flex-col gap-1 text-sm">
            {plan.pull_requests.map((p) => (
              <li key={p.number}>
                #{p.number} {p.title} <span className="text-[13px] text-muted-foreground">{p.state}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {plan.paths.length > 0 && (
        <div className="flex flex-col gap-1" data-testid="plan-paths">
          <h3 className="text-[13px] font-semibold">Paths it names</h3>
          <ul className="flex flex-col gap-0.5 font-mono text-[13px]">
            {plan.paths.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
          {plan.paths_total > plan.paths.length && (
            <p className="text-[13px] text-muted-foreground">and {plan.paths_total - plan.paths.length} more</p>
          )}
        </div>
      )}

      <Button size="sm" variant="ghost" className="self-start" onClick={onClose}>
        Show the list
      </Button>
    </section>
  );
}

function PlanCard({ plan, onOpen }: { plan: PlanRef; onOpen: () => void }) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className="flex w-full flex-col gap-1 rounded-lg border border-border bg-card p-4 text-left transition-colors hover:bg-muted"
      data-testid={`plan-card-${plan.stem}`}
    >
      <span className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">{plan.title}</span>
        <PlanStatusBadge status={plan.status} />
      </span>
      {plan.path && <span className="font-mono text-[13px] text-muted-foreground">{plan.path}</span>}
      {plan.objective && <span className="text-sm text-muted-foreground">{plan.objective}</span>}
      {plan.present === false && <span className="text-[13px] text-muted-foreground">No longer in the repository</span>}
    </button>
  );
}

/** `/p/<slug>/plans`: the committed plans, read-only. */
export function PlansPage() {
  const slug = useSlug();
  const navigate = useNavigate();
  const search = useSearch({ strict: false }) as PlansSearch;
  const { status, q, stem, section } = search;

  const go = (next: PlansSearch) =>
    void navigate({ to: "/p/$slug/plans", params: { slug }, search: next, replace: true });

  // The search box: typing waits 300 ms before it writes `q` to the URL.
  const [text, setText] = useState(q ?? "");
  const pushed = useRef(q);
  const debounced = useDebounced(text, 300);
  useEffect(() => {
    const next = debounced.trim() || undefined;
    if (next === pushed.current) return;
    pushed.current = next;
    go({ status, q: next, stem, section });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debounced]);
  useEffect(() => {
    // The URL changed from outside the box (Clear filters, back button): follow it.
    if (q !== pushed.current) {
      pushed.current = q;
      setText(q ?? "");
    }
  }, [q]);

  const list = useInfiniteQuery({
    queryKey: [...projectKey(slug, "plans", "list"), status ?? "all", q ?? ""],
    queryFn: ({ pageParam }) => projectApi(slug).plans({ q, status, limit: PAGE, offset: pageParam }),
    initialPageParam: 0,
    getNextPageParam: (last, all) => {
      const loaded = all.reduce((n, page) => n + page.plans.length, 0);
      return loaded < last.total ? loaded : undefined;
    },
    retry: false,
  });
  const detail = useProjectQuery(["plans", "one", stem], (api) => api.plan(stem!), {
    enabled: stem !== undefined,
    retry: false,
  });

  const plans = list.data?.pages.flatMap((p) => p.plans) ?? [];
  const dir = list.data?.pages[0]?.dir ?? "";
  const filtered = status !== undefined || q !== undefined;

  return (
    <PageContainer width="default" className="flex flex-col gap-5">
      <div>
        <h1 className="text-[22px] font-semibold tracking-tight">Plans</h1>
        <p className="text-[13px] text-muted-foreground">
          Plans your team committed to {dir ? <span className="font-mono">{dir}</span> : "the plans folder"}. Status comes
          from the commits and PRs that name them.
        </p>
      </div>

      {stem !== undefined && (
        <>
          {detail.isLoading && <TableSkeleton rows={2} cols={1} label="Loading the plan" />}
          {detail.isError && <ErrorState error={detail.error} title="Couldn't load that plan" size="inline" />}
          {detail.data && (
            <PlanDetail
              plan={detail.data}
              section={section}
              onClose={() => go({ status, q })}
            />
          )}
        </>
      )}

      <Tabs value={status ?? "all"} onValueChange={(v) => go({ status: v === "all" ? undefined : (v as PlanStatus), q })}>
        <TabsList variant="scrollable" aria-label="Plan status">
          <TabsTrigger value="all" data-testid="status-all">
            All
          </TabsTrigger>
          {PLAN_STATUSES.map((s) => (
            <TabsTrigger key={s} value={s} data-testid={`status-${s}`}>
              {STATUS_LABEL[s]}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      <div className="flex flex-wrap items-center gap-3">
        <Input
          type="search"
          aria-label="Search plans"
          placeholder="Search plans"
          className="max-w-sm"
          value={text}
          maxLength={200}
          onChange={(e) => setText(e.target.value)}
          data-testid="plans-search"
        />
        {filtered && <ClearFilters onClear={() => go({ stem, section })} />}
      </div>

      {list.isLoading && <TableSkeleton rows={4} cols={1} label="Loading plans" />}
      {list.isError && (
        <ErrorState error={list.error} title="Couldn't load the plans" onRetry={() => void list.refetch()} />
      )}
      {list.isSuccess && plans.length === 0 && (
        filtered ? (
          <EmptyState
            icon={<ClipboardListIcon />}
            title="No matches"
            description="No plans match these filters."
            action={<ClearFilters onClear={() => go({ stem, section })} />}
          />
        ) : dir === "" ? (
          <EmptyState
            icon={<ClipboardListIcon />}
            title="Plan indexing is off"
            description="Set a plans folder in Settings, Plans."
          />
        ) : (
          <EmptyState
            icon={<ClipboardListIcon />}
            title="No plans yet"
            description={`No plans in ${dir} yet. Publish one with /whygraph-plan --publish <stem>, commit it, and the next scan picks it up.`}
          />
        )
      )}
      {plans.length > 0 && (
        <ul className="flex flex-col gap-3" data-testid="plans-list">
          {plans.map((p) => (
            <li key={p.stem}>
              <PlanCard plan={p} onOpen={() => go({ status, q, stem: p.stem })} />
            </li>
          ))}
        </ul>
      )}
      {list.hasNextPage && (
        <Button
          variant="outline"
          className="self-start"
          disabled={list.isFetchingNextPage}
          onClick={() => void list.fetchNextPage()}
        >
          Load more
        </Button>
      )}
    </PageContainer>
  );
}
