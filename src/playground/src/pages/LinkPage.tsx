import { PageContainer } from "../components/layout/PageContainer";
import { useEffect, useState } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useMutation, useQuery } from "@tanstack/react-query";
import { TriangleAlertIcon } from "lucide-react";
import { platformApi, portalApi, portalKey } from "../api";
import { linkError } from "../lib/errors";
import { hardNavigate } from "../lib/navigation";
import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { CheckoutHint } from "../components/portal/CheckoutHint";
import { Field } from "../components/portal/Field";

export interface LinkSearch {
  platform?: string;
  org?: string;
  project?: string;
  /** The platform card's one-time intent: forwarded on Connect, never shown, stripped from the address. */
  intent?: string;
}

const INTENT_RE = /^[A-Za-z0-9_-]{43}$/;

/** The platform's origin from the `platform=` parameter, or `null` when it is not an http(s) address. */
export function parseLinkPlatform(raw: string | undefined): string | null {
  if (!raw) return null;
  try {
    const u = new URL(raw);
    return u.protocol === "https:" || u.protocol === "http:" ? u.origin : null;
  } catch {
    return null;
  }
}

/**
 * `/link?platform=<origin>&org=<org>&project=<slug>`: the page a platform's
 * "Open in my local WhyGraph" lands on. It is reachable from a link on *another*
 * site, so it **acts on nothing by itself**: it shows what it was asked to do -
 * the platform's full address, the org and project, the machine name - and
 * waits. Only the Connect button talks to the portal, and the browser then goes
 * to the consent page the portal built.
 */
export function LinkPage() {
  const search = useSearch({ strict: false }) as LinkSearch;
  const navigate = useNavigate();
  const origin = parseLinkPlatform(search.platform);
  // Read once; a malformed one is dropped silently. A reload loses it (the platform then shows Allow).
  const [intent] = useState(() => (search.intent && INTENT_RE.test(search.intent) ? search.intent : undefined));
  // History hygiene: the intent leaves the address (and so the history) at once.
  useEffect(() => {
    if (search.intent !== undefined) {
      void navigate({
        to: "/link",
        search: { platform: search.platform, org: search.org, project: search.project },
        replace: true,
      });
    }
  }, [search.intent, search.platform, search.org, search.project, navigate]);
  // Refetched on focus, so coming back from the terminal re-checks the shared folders.
  const state = useQuery({ queryKey: portalKey("state"), queryFn: portalApi.state, refetchOnWindowFocus: true });
  const shared = state.data?.shared_folders;
  // What this machine already holds of the requested project (a platform-initiated link names it).
  const projects = useQuery({ queryKey: portalKey("projects"), queryFn: portalApi.projects });
  const checkable = !!origin && !!search.org && !!search.project && projects.isSuccess;
  const existing = checkable
    ? projects.data.projects.find(
        (p) =>
          p.link?.platform_origin === origin && p.link.org === search.org && p.link.remote_slug === search.project,
      )
    : undefined;
  // A revoked link is reconnected in place: the checkout is already there, so no hint and no gate.
  const reconnecting = existing?.link?.status === "revoked";
  const alreadyLinked = !!existing && !reconnecting;
  // A linked project keeps the platform's slug, so a project of that name blocks the link.
  const clash = checkable && !existing ? projects.data.projects.find((p) => p.slug === search.project) : undefined;
  const noFolder = !reconnecting && shared?.length === 0;
  const [name, setName] = useState<string | null>(null);
  const shownName = name ?? state.data?.hostname ?? "";
  const connect = useMutation({
    mutationFn: () =>
      platformApi.connect({
        platform_url: origin!,
        client_name: shownName.trim() || undefined,
        ...(search.org && { org: search.org }),
        ...(search.project && { project: search.project }),
        ...(intent && { intent }),
      }),
    onSuccess: ({ authorize_url }) => void hardNavigate(authorize_url),
  });

  return (
    <PageContainer width="narrow" className="flex flex-col gap-5">
      <div className="flex flex-col gap-1">
        <h1 className="text-[22px] font-semibold tracking-tight">Link a checkout to a platform project</h1>
        <p className="text-[13px] text-muted-foreground">
          A WhyGraph platform asked this portal to connect. Nothing happens until you press Connect.
        </p>
      </div>

      {!origin ? (
        <Alert variant="destructive" data-testid="link-invalid">
          <AlertTitle>This link is incomplete</AlertTitle>
          <AlertDescription>
            It does not name a platform address. Open the link again from the platform, or{" "}
            <Link to="/projects/new" search={{ source: "platform" }} className="underline">
              enter the address yourself
            </Link>
            .
          </AlertDescription>
        </Alert>
      ) : (
        <form
          className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5 shadow-card"
          onSubmit={(e) => {
            e.preventDefault();
            connect.mutate();
          }}
        >
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm" data-testid="link-request">
            <dt className="text-muted-foreground">Platform</dt>
            <dd className="break-all font-mono text-xs" data-testid="link-platform">
              {origin}
            </dd>
            {search.org && (
              <>
                <dt className="text-muted-foreground">Organization</dt>
                <dd className="font-mono text-xs">{search.org}</dd>
              </>
            )}
            {search.project && (
              <>
                <dt className="text-muted-foreground">Project</dt>
                <dd className="font-mono text-xs">{search.project}</dd>
              </>
            )}
          </dl>
          <p className="text-xs text-muted-foreground">
            {intent
              ? "You may need to sign in on the platform. Check the address above is one you trust - if you did not start this there, press Cancel."
              : "You will sign in on the platform and allow this machine to read that project's history. Check the address above is one you trust."}
          </p>
          {alreadyLinked && existing && (
            <Alert data-testid="already-linked">
              <AlertTitle>Already linked on this machine</AlertTitle>
              <AlertDescription>
                <p>
                  {search.org}/{search.project} is the project <strong>{existing.slug}</strong> here.
                </p>
                <div className="mt-2">
                  <Button size="sm" render={<Link to="/p/$slug" params={{ slug: existing.slug }} />}>
                    Open project
                  </Button>
                </div>
              </AlertDescription>
            </Alert>
          )}
          {clash && (
            <Alert variant="warning" data-testid="slug-clash" id="slug-clash">
              <TriangleAlertIcon />
              <AlertTitle>A project named {clash.slug} is already on this machine</AlertTitle>
              <AlertDescription>
                <p>
                  A linked project keeps the platform's name, so remove that project first (leave Also delete
                  history unticked to keep its history), then connect.
                </p>
                <div className="mt-2">
                  <Button
                    size="sm"
                    variant="outline"
                    render={
                      <Link to="/p/$slug/settings" params={{ slug: clash.slug }} search={{ section: "danger" }} />
                    }
                  >
                    Remove {clash.slug}
                  </Button>
                </div>
              </AlertDescription>
            </Alert>
          )}
          <Field label="Machine name" hint="Shown on the platform, so you can tell this machine apart.">
            {(p) => <Input {...p} value={shownName} onChange={(e) => setName(e.target.value)} />}
          </Field>
          {!reconnecting && (
            <CheckoutHint shared={shared} onCheckAgain={() => void state.refetch()} checking={state.isFetching} />
          )}
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
              variant={alreadyLinked ? "outline" : "default"}
              disabled={!shownName.trim() || connect.isPending || noFolder || !!clash}
              aria-describedby={clash ? "slug-clash" : noFolder ? "no-shared-folders" : undefined}
            >
              {connect.isPending
                ? "Connecting…"
                : reconnecting
                  ? "Reconnect"
                  : alreadyLinked
                    ? "Connect again"
                    : "Connect"}
            </Button>
          </div>
        </form>
      )}
    </PageContainer>
  );
}
