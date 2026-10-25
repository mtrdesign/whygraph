import { DisabledReason } from "../state/DisabledReason";
import { useState, type ReactNode } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
import { CheckoutHint } from "./CheckoutHint";
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
  // Refetched on focus, so coming back from the terminal re-checks the shared folders.
  const state = useQuery({ queryKey: portalKey("state"), queryFn: portalApi.state, refetchOnWindowFocus: true });
  const shared = state.data?.shared_folders;
  const projects = useQuery({ queryKey: portalKey("projects"), queryFn: portalApi.projects });
  // The platforms this portal already links projects to: one is offered as the address, several as buttons.
  const origins = [...new Set((projects.data?.projects ?? []).flatMap((p) => p.link?.platform_origin ?? []))];
  const [url, setUrl] = useState<string | null>(initialUrl || null);
  const shownUrl = url ?? (origins.length === 1 ? origins[0] : "");
  const [name, setName] = useState<string | null>(initialName ?? null);
  const shownName = name ?? state.data?.hostname ?? "";
  const connect = useMutation({
    mutationFn: () =>
      platformApi.connect({
        platform_url: shownUrl.trim(),
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
        if (shownUrl.trim()) connect.mutate();
      }}
    >
      <Field
        label="Platform address"
        hint="Your WhyGraph platform's address, for example https://whygraph.example.com. Your organization's address (https://acme.whygraph.example.com) works too."
      >
        {(p) => (
          <Input
            {...p}
            type="url"
            placeholder="https://whygraph.example.com"
            value={shownUrl}
            onChange={(e) => setUrl(e.target.value)}
          />
        )}
      </Field>
      {origins.length > 1 && (
        <div
          data-testid="known-platforms"
          role="group"
          aria-label="Platforms you already use"
          className="flex flex-wrap gap-2"
        >
          {origins.map((origin) => (
            <Button
              key={origin}
              type="button"
              variant="outline"
              size="sm"
              className="max-w-full truncate"
              onClick={() => setUrl(origin)}
            >
              {platformHost(origin)}
            </Button>
          ))}
        </div>
      )}
      <Field label="Machine name" hint="Shown on the platform, so you can tell this machine's connection apart.">
        {(p) => <Input {...p} value={shownName} onChange={(e) => setName(e.target.value)} />}
      </Field>
      <CheckoutHint shared={shared} onCheckAgain={() => void state.refetch()} checking={state.isFetching} />
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
        <Button
          type="submit"
          disabled={!shownUrl.trim() || !shownName.trim() || connect.isPending || shared?.length === 0}
          aria-describedby={shared?.length === 0 ? "no-shared-folders" : undefined}
        >
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
  disabled = false,
  footer,
}: {
  path: string;
  name: string;
  /** A short badge beside the name, for example the already-linked checkout. */
  note?: string;
  selected: boolean;
  onSelect: () => void;
  /** A row that is shown but cannot be picked (a checkout that is already a project here). */
  disabled?: boolean;
  /** A line under the row, outside its label (a link cannot sit inside one). */
  footer?: ReactNode;
}) {
  return (
    <div className="border-b border-border last:border-b-0">
      <label
        className={`flex items-center gap-3 px-3.5 py-2.5 ${
          disabled ? "cursor-not-allowed opacity-70" : "cursor-pointer hover:bg-muted/50"
        }`}
      >
        <input
          type="radio"
          name="checkout"
          className="accent-primary"
          checked={selected}
          onChange={onSelect}
          disabled={disabled}
          aria-disabled={disabled || undefined}
        />
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
      {footer && <div className="px-3.5 pb-2.5 pl-10">{footer}</div>}
    </div>
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
  // Picked by the user, else the one sensible default: the reconnect target, or the only origin match.
  const [path, setPath] = useState<string | null>(null);
  const picked = path ?? reconnect?.path ?? (pending.candidates.length === 1 ? pending.candidates[0].path : "");
  const [typed, setTyped] = useState("");
  const chosen = typed.trim() || picked;
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
  // A checkout that is already a project here is not "not found".
  const none = pending.candidates.length === 0 && !reconnect && pending.already_projects.length === 0;
  const several = pending.clone_commands.length > 1;
  const where = several ? "your shared folders" : "your shared folder";
  const others = pending.other_repos.length > 0;

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
              selected={picked === reconnect.path && !typed.trim()}
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
              selected={picked === c.path && !typed.trim()}
              onSelect={() => {
                setPath(c.path);
                setTyped("");
              }}
            />
          ))}
          {pending.already_projects.map((a) => (
            <PathRow
              key={a.path}
              path={a.path}
              name={a.path.split("/").filter(Boolean).pop() ?? a.path}
              note="already a project here"
              selected={false}
              onSelect={() => {}}
              disabled
              footer={
                <p className="text-xs text-muted-foreground" data-testid="already-project-note">
                  This is the project{" "}
                  <Link
                    to="/p/$slug/settings"
                    params={{ slug: a.slug }}
                    search={{ section: "danger" }}
                    className="text-primary-text hover:underline"
                  >
                    {a.slug}
                  </Link>{" "}
                  on this portal. Remove it first (leave Also delete history unticked to keep its history), then
                  link again.
                </p>
              }
            />
          ))}
          {none && (
            <div className="flex flex-col gap-2 p-3.5 text-sm" data-testid="no-candidates">
              <p className="text-muted-foreground">
                {pending.clone_commands.length === 0
                  ? "No folder is shared with the portal."
                  : `No checkout of ${pending.project.name} found under ${where}. ${
                      others
                        ? "If one of the other repositories below is a checkout of it, pick it there. Otherwise clone it"
                        : "Clone it"
                    } ${several ? "into one of them" : "there"}, then check again:`}
              </p>
              {pending.clone_commands.map((c) => (
                <CommandBlock key={c.folder} command={c.command} />
              ))}
              <div>
                <Button variant="outline" size="sm" onClick={onRefresh} disabled={refreshing}>
                  {refreshing ? "Checking…" : "Check again"}
                </Button>
              </div>
              <p className="text-xs text-muted-foreground" data-testid="add-folder-note">
                Cloning somewhere else? Share that folder first with{" "}
                <code>whygraph up --add-folder &lt;folder&gt;</code>. That restarts the portal and drops this
                connection, so you connect again afterwards.
              </p>
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
                selected={picked === c.path && !typed.trim()}
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
  const queryClient = useQueryClient();
  const pendingKey = portalKey("platform-pending", linkId ?? "");
  const pending = useQuery({
    queryKey: pendingKey,
    queryFn: () => platformApi.pending(linkId!),
    enabled: !!linkId,
    retry: false,
    refetchOnWindowFocus: false,
  });
  // Check again: search the shared folders afresh and re-match origins (the server clears its cache).
  const refresh = useMutation({
    mutationFn: () => platformApi.pending(linkId!, true),
    onSuccess: (data) => queryClient.setQueryData(pendingKey, data),
    onError: () => void pending.refetch(), // an expired link then shows the pending-error state
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
            {pending.data ? (
              <Button
                variant="outline"
                render={
                  <Link
                    to="/link"
                    search={{
                      platform: pending.data.platform_origin,
                      org: pending.data.org,
                      project: pending.data.project.slug,
                    }}
                  />
                }
              >
                Start again
              </Button>
            ) : (
              <Button variant="outline" render={<Link to="/projects/new" search={{ source: "platform" }} />}>
                Start again
              </Button>
            )}
          </div>
        </div>
      )}
      {pending.data && !pending.isError && (
        <Picker
          pending={pending.data}
          onAdded={onAdded}
          onRefresh={() => refresh.mutate()}
          refreshing={refresh.isPending}
        />
      )}
    </div>
  );
}
