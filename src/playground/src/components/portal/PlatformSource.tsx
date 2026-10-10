import { DisabledReason } from "../state/DisabledReason";
import { useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useMutation, useQuery } from "@tanstack/react-query";
import { CheckCircle2Icon } from "lucide-react";
import {
  ApiError,
  platformApi,
  portalApi,
  portalKey,
  type AddProjectResult,
  type PendingPlatformLink,
} from "../../api";
import { linkError, linkErrorMessage } from "../../lib/errors";
import { hardNavigate } from "../../lib/navigation";
import { platformHost } from "../../lib/platformLink";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { CommandBlock } from "../layout/CommandBlock";
import { PathText } from "../layout/PathText";
import { Field } from "./Field";

/**
 * The connect form (step 1 of linking, no link yet): the platform's address and
 * the machine name it will show. Connect sends the browser to the platform's own
 * consent page - the address comes from the portal, never composed here.
 */
export function ConnectForm({
  initialUrl = "",
  initialName,
  org,
  project,
}: {
  initialUrl?: string;
  initialName?: string;
  org?: string;
  project?: string;
}) {
  const state = useQuery({ queryKey: portalKey("state"), queryFn: portalApi.state });
  const [url, setUrl] = useState(initialUrl);
  const [name, setName] = useState<string | null>(initialName ?? null);
  const shownName = name ?? state.data?.hostname ?? "";
  const connect = useMutation({
    mutationFn: () =>
      platformApi.connect({
        platform_url: url.trim(),
        client_name: shownName.trim() || undefined,
        ...(org && { org }),
        ...(project && { project }),
      }),
    onSuccess: ({ authorize_url }) => void hardNavigate(authorize_url),
  });

  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (url.trim()) connect.mutate();
      }}
    >
      <Field
        label="Platform address"
        hint="The address of your organization's WhyGraph platform, for example https://whygraph.example.com."
      >
        {(p) => (
          <Input
            {...p}
            type="url"
            placeholder="https://whygraph.example.com"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />
        )}
      </Field>
      <Field label="Machine name" hint="Shown on the platform, so you can tell this machine's connection apart.">
        {(p) => <Input {...p} value={shownName} onChange={(e) => setName(e.target.value)} />}
      </Field>
      {connect.isError && (
        <Alert variant="destructive" data-testid="connect-error">
          <AlertTitle>Could not connect</AlertTitle>
          <AlertDescription>{linkError(connect.error)}</AlertDescription>
        </Alert>
      )}
      <div className="flex items-center justify-end gap-2">
        <Button variant="ghost" render={<Link to="/" />}>
          Cancel
        </Button>
        <Button type="submit" disabled={!url.trim() || !shownName.trim() || connect.isPending}>
          {connect.isPending ? "Connecting…" : "Connect"}
        </Button>
      </div>
    </form>
  );
}

function PathRow({
  path,
  name,
  note,
  selected,
  onSelect,
}: {
  path: string;
  name: string;
  /** A short badge beside the name, for example the already-linked checkout. */
  note?: string;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <label className="flex cursor-pointer items-center gap-3 border-b border-border px-3.5 py-2.5 last:border-b-0 hover:bg-muted/50">
      <input type="radio" name="checkout" className="accent-primary" checked={selected} onChange={onSelect} />
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="font-medium">
          {name}
          {note && (
            <span className="ml-2 rounded-full bg-muted px-2 py-0.5 text-xs font-normal text-muted-foreground">
              {note}
            </span>
          )}
        </span>
        {/* Truncated from the start, so the distinguishing tail stays (PH-8). */}
        <span className="flex min-w-0 text-muted-foreground">
          <PathText path={path} />
        </span>
      </span>
    </label>
  );
}

function Picker({
  pending,
  onAdded,
  onRefresh,
  refreshing,
}: {
  pending: PendingPlatformLink;
  onAdded: (r: AddProjectResult) => void;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  const navigate = useNavigate();
  const reconnect = pending.reconnect;
  // The one sensible pick when this link reconnects, so it starts selected.
  const [path, setPath] = useState(reconnect?.path ?? "");
  const [typed, setTyped] = useState("");
  const chosen = typed.trim() || path;
  const reconnecting = !!reconnect && chosen === reconnect.path;
  // A name collision this link cannot reconnect is the only dead end.
  const blocked = pending.slug_taken && !reconnect;
  const add = useMutation({
    mutationFn: () => portalApi.addProject({ source: "platform", link_id: pending.link_id, path: chosen }),
    onSuccess: onAdded,
  });
  const abandon = useMutation({
    mutationFn: () => platformApi.abandon(pending.link_id),
    // Gone either way (an expired link has nothing left to revoke): back to a clean form.
    onSettled: () => void navigate({ to: "/projects/new", search: { source: "platform" } }),
  });
  const none = pending.candidates.length === 0 && !reconnect;

  return (
    <div className="flex flex-col gap-4" data-testid="platform-picker">
      <p className="flex items-start gap-2 text-sm" data-testid="platform-connected">
        <CheckCircle2Icon className="mt-0.5 size-4 shrink-0 text-success" />
        {/* One sentence that wraps at phone width, not three squeezed columns. */}
        <span className="min-w-0 wrap-break-word">
          Connected to{" "}
          <span className="font-medium">
            {pending.org}/{pending.project.name}
          </span>{" "}
          on {platformHost(pending.platform_origin)}
        </span>
      </p>
      {blocked && (
        <Alert variant="destructive" data-testid="slug-taken">
          <AlertTitle>Already on this machine</AlertTitle>
          <AlertDescription>
            {linkErrorMessage(new ApiError(409, "", "slug_taken"))} Remove that project first, then link
            again.
          </AlertDescription>
        </Alert>
      )}
      {reconnect && (
        <Alert data-testid="reconnect-notice">
          <AlertTitle>Already linked on this machine</AlertTitle>
          <AlertDescription>
            This checkout is already linked to {pending.org}/{pending.project.name}. Linking it again
            reconnects it: it gets a new connection token and access is restored. Nothing else about the
            project changes - the same project row, its index and its agent wiring stay as they are.
          </AlertDescription>
        </Alert>
      )}

      <div className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold">Checkouts of this repository</h2>
        <div className="flex flex-col rounded-lg border border-border" role="radiogroup" aria-label="Checkout">
          {reconnect && (
            <PathRow
              path={reconnect.path}
              name={reconnect.slug}
              note="already linked"
              selected={path === reconnect.path && !typed.trim()}
              onSelect={() => {
                setPath(reconnect.path);
                setTyped("");
              }}
            />
          )}
          {pending.candidates.map((c) => (
            <PathRow
              key={c.path}
              {...c}
              selected={path === c.path && !typed.trim()}
              onSelect={() => {
                setPath(c.path);
                setTyped("");
              }}
            />
          ))}
          {none && (
            <div className="flex flex-col gap-2 p-3.5 text-sm" data-testid="no-candidates">
              <p className="text-muted-foreground">
                No checkout of {pending.project.name} found under your shared folders.{" "}
                {pending.other_repos.length > 0
                  ? "If one of the other repositories below is a checkout of it, pick it there. Otherwise clone it into a shared folder, then check again:"
                  : "Clone it into a shared folder, then check again:"}
              </p>
              <CommandBlock command={pending.clone_command} />
              <div>
                <Button variant="outline" size="sm" onClick={onRefresh} disabled={refreshing}>
                  {refreshing ? "Checking…" : "Check again"}
                </Button>
              </div>
            </div>
          )}
        </div>
      </div>

      {pending.other_repos.length > 0 && (
        <div className="flex flex-col gap-2">
          <h2 className="text-sm font-semibold">Other repositories</h2>
          <p className="text-xs text-muted-foreground">
            Their origin does not match. One is accepted only if it contains the commit the platform last
            scanned.
          </p>
          <div className="flex max-h-56 flex-col overflow-y-auto rounded-lg border border-border" role="radiogroup" aria-label="Other repositories">
            {pending.other_repos.map((c) => (
              <PathRow
                key={c.path}
                {...c}
                selected={path === c.path && !typed.trim()}
                onSelect={() => {
                  setPath(c.path);
                  setTyped("");
                }}
              />
            ))}
          </div>
        </div>
      )}

      <Field label="Or enter a path" hint="A folder inside one of your shared folders.">
        {(p) => (
          <Input
            {...p}
            className="font-mono"
            placeholder="/Users/you/Work/my-repo"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
          />
        )}
      </Field>

      {add.isError && (
        <Alert variant="destructive" data-testid="link-error">
          <AlertTitle>Could not link the checkout</AlertTitle>
          <AlertDescription>{linkError(add.error)}</AlertDescription>
        </Alert>
      )}
      <div className="flex items-center justify-end gap-2">
        <Button variant="ghost" onClick={() => abandon.mutate()} disabled={abandon.isPending}>
          Cancel
        </Button>
        {(() => {
          const label = add.isPending
            ? reconnecting
              ? "Reconnecting…"
              : "Linking…"
            : reconnecting
              ? "Reconnect this checkout"
              : "Link this checkout";
          return chosen || add.isPending ? (
            <Button onClick={() => add.mutate()} disabled={!chosen || blocked || add.isPending}>
              {label}
            </Button>
          ) : (
            <DisabledReason reason="Choose a checkout from the list, or enter a path to one.">
              <Button disabled>{label}</Button>
            </DisabledReason>
          );
        })()}
      </div>
    </div>
  );
}

/**
 * Platform source of the add wizard. Without a `link` it is the connect form; with
 * one (the callback page put it in the address) it is the checkout picker, which
 * ends in `POST /api/projects {source: "platform"}` and then the wizard's
 * Initialize step (Configure is skipped: the platform owns the project's config).
 */
export function PlatformSource({
  linkId,
  onAdded,
}: {
  linkId?: string;
  onAdded: (r: AddProjectResult) => void;
}) {
  const pending = useQuery({
    queryKey: portalKey("platform-pending", linkId ?? ""),
    queryFn: () => platformApi.pending(linkId!),
    enabled: !!linkId,
    retry: false,
    refetchOnWindowFocus: false,
  });
  if (!linkId) {
    return (
      <div className="p-5">
        <ConnectForm />
      </div>
    );
  }
  return (
    <div className="p-5">
      {pending.isLoading && <p className="text-sm text-muted-foreground">Loading…</p>}
      {pending.isError && (
        <div className="flex flex-col gap-3" data-testid="pending-error">
          <Alert variant="destructive">
            <AlertTitle>This link cannot continue</AlertTitle>
            <AlertDescription>{linkError(pending.error)}</AlertDescription>
          </Alert>
          <div>
            <Button variant="outline" render={<Link to="/projects/new" search={{ source: "platform" }} />}>
              Start again
            </Button>
          </div>
        </div>
      )}
      {pending.data && (
        <Picker
          pending={pending.data}
          onAdded={onAdded}
          onRefresh={() => void pending.refetch()}
          refreshing={pending.isFetching}
        />
      )}
    </div>
  );
}
