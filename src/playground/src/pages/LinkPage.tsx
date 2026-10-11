import { PageContainer } from "../components/layout/PageContainer";
import { useState } from "react";
import { Link, useSearch } from "@tanstack/react-router";
import { useMutation, useQuery } from "@tanstack/react-query";
import { platformApi, portalApi, portalKey } from "../api";
import { linkError } from "../lib/errors";
import { hardNavigate } from "../lib/navigation";
import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Field } from "../components/portal/Field";

export interface LinkSearch {
  platform?: string;
  org?: string;
  project?: string;
}

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
  const origin = parseLinkPlatform(search.platform);
  const state = useQuery({ queryKey: portalKey("state"), queryFn: portalApi.state });
  const [name, setName] = useState<string | null>(null);
  const shownName = name ?? state.data?.hostname ?? "";
  const connect = useMutation({
    mutationFn: () =>
      platformApi.connect({
        platform_url: origin!,
        client_name: shownName.trim() || undefined,
        ...(search.org && { org: search.org }),
        ...(search.project && { project: search.project }),
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
            You will sign in on the platform and allow this machine to read that project's history. Check
            the address above is one you trust.
          </p>
          <Field label="Machine name" hint="Shown on the platform, so you can tell this machine apart.">
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
            <Button type="submit" disabled={!shownName.trim() || connect.isPending}>
              {connect.isPending ? "Connecting…" : "Connect"}
            </Button>
          </div>
        </form>
      )}
    </PageContainer>
  );
}
