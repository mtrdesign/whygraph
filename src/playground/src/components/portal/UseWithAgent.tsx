import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { connectApi, type MyProjectConnection } from "../../api";
import { useReadOnly } from "../../lib/identity";
import { DEFAULT_LOCAL_PORT, localLinkUrl, parsePort, readStoredPort, storePort } from "../../lib/connections";
import { accountUrl } from "../../lib/platformLink";
import { timeAgo } from "../../lib/projectStatus";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { Skeleton } from "../ui/skeleton";
import { CopyButton } from "./CopyButton";
import { When } from "./When";

/**
 * The caller's own machines linked to one project. Always fresh (`staleTime: 0`)
 * and refetched when the tab regains focus, so the machine appears the moment the
 * person comes back from the local portal (the app default is neither). The
 * `"@project"` prefix lets an admin's revoke in the project's Settings refresh it.
 */
export function useMyMachines(slug: string, enabled: boolean) {
  return useQuery({
    queryKey: ["@project", slug, "connections", "mine"],
    queryFn: () => connectApi.myProjectConnections(slug),
    enabled,
    staleTime: 0,
    refetchOnWindowFocus: true,
    retry: false,
  });
}

/**
 * The caller's machines for a project. A row never used is a link the person
 * approved here but did not finish on that machine.
 */
export function MyMachines({ machines, baseUrl }: { machines: MyProjectConnection[]; baseUrl?: string | null }) {
  const account = accountUrl(baseUrl);
  return (
    <ul className="flex flex-col divide-y divide-border" data-testid="my-machines">
      {machines.map((m) => {
        const pending = m.last_used_at === null;
        return (
          <li
            key={m.uid}
            className="row-wrap py-2 text-sm"
            data-testid="my-machine-row"
            data-state={pending ? "pending" : undefined}
          >
            <span className="min-w-0 flex-1 break-words">
              {pending ? (
                <>
                  Approved for <span className="font-medium">{m.client_name}</span> {timeAgo(m.created_at)} · not
                  linked yet - finish on that machine
                </>
              ) : (
                <>
                  Connected on <span className="font-medium">{m.client_name}</span>
                  <span className="text-xs text-muted-foreground">
                    {" · "}
                    <When label="linked" iso={m.created_at} />
                    {" · "}
                    <When label="last used" iso={m.last_used_at} />
                  </span>
                </>
              )}
            </span>
            {account && (
              <a href={account} className="shrink-0 text-xs text-primary-text hover:underline">
                Manage in Account
              </a>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/**
 * The install / start / open steps, the committed port (a detail, changed only
 * when it is not the default) and the button that opens the local portal's
 * `/link` page. This page never calls the local portal (no cross-origin probe),
 * it only hands over where to go. The port is remembered in `localStorage`, best
 * effort. While mounted (only while visible) it keeps a one-time intent for the
 * committed port in the link, so the platform's consent page can skip Allow; a
 * pending or failed mint yields a link without one (the Allow page, as before).
 */
function ConnectSteps({
  baseUrl,
  org,
  slug,
  repo,
  readOnly,
}: {
  baseUrl: string;
  org: string;
  slug: string;
  repo: string | null;
  readOnly: boolean;
}) {
  // `port` is always valid; `draft` is what the input holds while it is being edited.
  const [port, setPort] = useState(() => parsePort(readStoredPort()) ?? DEFAULT_LOCAL_PORT);
  const [draft, setDraft] = useState(() => String(port));
  const [editing, setEditing] = useState(false);
  const changeRef = useRef<HTMLButtonElement>(null);
  const refocus = useRef(false);
  const skipBlur = useRef(false);
  const draftPort = parsePort(draft);
  const origin = new URL(baseUrl).origin;
  const queryClient = useQueryClient();
  const intentKey = ["@project", slug, "connect-intent", port] as const;
  const intent = useQuery({
    queryKey: intentKey,
    queryFn: () => connectApi.connectIntent(slug, port),
    enabled: !readOnly, // `port` is always a valid number
    staleTime: 5 * 60_000,
    refetchInterval: 5 * 60_000,
    refetchOnWindowFocus: true,
    retry: false,
  });
  const href = localLinkUrl(port, origin, org, slug, intent.data?.intent);

  // Focus returns to Change once the input is gone (not on the first render).
  useEffect(() => {
    if (!editing && refocus.current) {
      refocus.current = false;
      changeRef.current?.focus();
    }
  }, [editing]);

  const commit = () => {
    if (draftPort === null) return;
    setPort(draftPort);
    storePort(draft.trim());
    refocus.current = true;
    setEditing(false);
  };
  const revert = () => {
    skipBlur.current = true;
    setDraft(String(port));
    refocus.current = true;
    setEditing(false);
  };

  return (
    <>
      <ol className="flex list-decimal flex-col gap-1.5 pl-5 text-sm">
        <li>
          Install WhyGraph on your machine - see the{" "}
          <a
            href="https://mtrdesign.github.io/whygraph/getting-started/installation/"
            target="_blank"
            rel="noopener noreferrer"
            className="text-primary-text hover:underline"
          >
            installation guide
          </a>
          .
        </li>
        <li data-testid="clone-step">
          Clone <strong>{repo ?? "this repository"}</strong> into the folder where you keep your repositories, if it
          is not there yet.
        </li>
        <li className="flex flex-wrap items-center gap-2">
          Start WhyGraph with that folder shared:{" "}
          <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">whygraph up --add-folder ~/Work</code>
          <CopyButton text="whygraph up --add-folder ~/Work" /> - use your folder; running it again with the same
          folder changes nothing.
        </li>
        <li>Press the button below, then pick that clone on your machine.</li>
      </ol>
      <div className="flex flex-col gap-2">
        <div>
          <Button
            render={
              <a
                href={href}
                target="_blank"
                rel="noopener noreferrer"
                // An intent is single use: the next click gets a fresh one.
                onClick={() => void queryClient.invalidateQueries({ queryKey: intentKey })}
              />
            }
          >
            Open in my local WhyGraph
          </Button>
        </div>
        <p className="text-xs text-muted-foreground" data-testid="connect-scope">
          This lets WhyGraph on port {port} of this computer read this project as you and, if your role allows, ask
          for rationale cards with your organization's AI keys. You confirm once more on your machine.
        </p>
        {editing ? (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="local-port">Port of your local WhyGraph</Label>
            <Input
              id="local-port"
              inputMode="numeric"
              autoFocus
              value={draft}
              aria-invalid={draftPort === null}
              aria-describedby={draftPort === null ? "local-port-error" : undefined}
              className="w-28 font-mono"
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  commit();
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  e.stopPropagation(); // revert the draft, not the dialog around it
                  revert();
                }
              }}
              onBlur={() => {
                if (skipBlur.current) {
                  skipBlur.current = false;
                  return;
                }
                commit();
              }}
            />
            {draftPort === null && (
              <p id="local-port-error" className="text-xs text-destructive">
                Enter a port from 1 to 65535.
              </p>
            )}
          </div>
        ) : (
          <p className="text-xs text-muted-foreground" data-testid="local-port-summary">
            Port {port} ·{" "}
            <button
              type="button"
              ref={changeRef}
              className="text-primary-text hover:underline"
              onClick={() => {
                skipBlur.current = false;
                setEditing(true);
              }}
            >
              Change
            </button>
          </p>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        If the new tab says the site can't be reached, start WhyGraph with{" "}
        <span className="font-mono">whygraph up</span> and reload that tab. This page does not check
        whether WhyGraph is running on your machine. On another port?{" "}
        <span className="font-mono">whygraph status</span> shows its address; press Change.
      </p>
    </>
  );
}

/**
 * "Connect local WhyGraph" (production project home, M2e plan section 4.6): link a
 * checkout on the reader's own machine to this project, so their coding agent
 * gets its evidence. `variant` says where it sits: `card` (the Overview, with a
 * title), `dialog` (inside `ConnectAgentDialog`, whose header already says it) or
 * `embedded` (inside the Overview's disclosure: only the steps).
 */
export function UseWithAgent({
  baseUrl,
  org,
  slug,
  repo,
  variant = "card",
}: {
  baseUrl: string;
  org: string;
  slug: string;
  /** `owner/name` of the project's repository, shown in the clone step; `null` when unknown. */
  repo: string | null;
  variant?: "card" | "dialog" | "embedded";
}) {
  const readOnly = useReadOnly();
  const mine = useMyMachines(slug, !readOnly && variant !== "embedded");
  const [another, setAnother] = useState(false);
  const steps = <ConnectSteps baseUrl={baseUrl} org={org} slug={slug} repo={repo} readOnly={readOnly} />;
  if (variant === "embedded") return steps;

  const machines = mine.data ?? [];
  // Mid-flow (every row approved but never used) reads like no machine: the steps stay open.
  const expanded = machines.length === 0 || machines.every((m) => m.last_used_at === null);
  const body = (
    <>
      {machines.length > 0 && <MyMachines machines={machines} baseUrl={baseUrl} />}
      {mine.isLoading ? (
        <Skeleton className="h-9" />
      ) : expanded ? (
        steps
      ) : (
        <details
          className="text-sm"
          data-testid="connect-another"
          onToggle={(e) => setAnother(e.currentTarget.open)}
        >
          <summary className="cursor-pointer text-xs text-primary-text select-none hover:underline">
            Connect another machine
          </summary>
          {another && <div className="mt-3 flex flex-col gap-3">{steps}</div>}
        </details>
      )}
    </>
  );

  if (variant === "dialog") {
    return (
      <div className="flex flex-col gap-3" data-testid="use-with-agent-dialog">
        {body}
      </div>
    );
  }
  return (
    <section
      className="flex flex-col gap-3 rounded-xl border border-border bg-card p-5 shadow-card"
      data-testid="use-with-agent"
    >
      <div>
        <h2 className="text-sm font-semibold">Connect local WhyGraph</h2>
        <p className="text-xs text-muted-foreground">
          Link a checkout of this repository on your machine to this project. Your coding agent then
          reads this project's history through your local WhyGraph; your uncommitted changes stay on
          your machine.
        </p>
      </div>
      {body}
    </section>
  );
}
