import { useEffect, useState } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ApiError, connectApi, type ConnectRequest } from "../api";
import { AuthLayout } from "../components/auth/AuthLayout";
import { Field, nativeSelectClass } from "../components/portal/Field";
import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Skeleton } from "../components/ui/skeleton";
import { linkError } from "../lib/errors";
import { hardNavigate } from "../lib/navigation";

/** The consent query's keys: what `/connect?...` carries from the local portal. */
export type ConnectSearch = Partial<Record<keyof ConnectRequest, string>>;

const SAVED_KEY = "whygraph.connect";

// The query survives the GitHub sign-in round trip in `sessionStorage` (best
// effort: storage may be blocked), and is stripped from the address only once the
// server has validated it for a signed-in session.
function saveRequest(req: ConnectRequest): void {
  try {
    window.sessionStorage.setItem(SAVED_KEY, JSON.stringify(req));
  } catch {
    // the address itself still carries the request
  }
}

function savedRequest(): ConnectRequest | null {
  try {
    const raw = window.sessionStorage.getItem(SAVED_KEY);
    return raw ? complete(JSON.parse(raw) as ConnectSearch) : null;
  } catch {
    return null;
  }
}

function forgetRequest(): void {
  try {
    window.sessionStorage.removeItem(SAVED_KEY);
  } catch {
    // nothing to forget
  }
}

/** The request when every required field is present, else `null` (the server judges validity). */
export function complete(q: ConnectSearch): ConnectRequest | null {
  const { redirect_uri, code_challenge, code_challenge_method, state, client_name } = q;
  if (!redirect_uri || !code_challenge || !code_challenge_method || !state || !client_name) return null;
  const req: ConnectRequest = { redirect_uri, code_challenge, code_challenge_method, state, client_name };
  if (q.org) req.org = q.org;
  if (q.project) req.project = q.project;
  return req;
}

function Refused({ error }: { error: unknown }) {
  // The server's words; for a refused request there is no URL to go back to.
  const message =
    error instanceof ApiError && error.code === "bad_connect_request"
      ? `${linkError(error)} (${error.message})`
      : linkError(error);
  return (
    <AuthLayout title="Connection request refused">
      <Alert variant="destructive" data-testid="connect-refused">
        <AlertTitle>This request cannot be approved</AlertTitle>
        <AlertDescription>
          <p>{message}</p>
          <p className="mt-1">You can close this tab and start again from your local WhyGraph.</p>
        </AlertDescription>
      </Alert>
    </AuthLayout>
  );
}

/**
 * `/connect` (base host, signed in; M2e plan section 4.4): a local WhyGraph asks to
 * read one project on the signed-in user's behalf. The page shows what the server
 * validated and lets the user pick a project they are a member of. **It builds no
 * URL**: Cancel goes to the `cancel_url` the validate call returned and Allow to
 * the `redirect` the authorize call returned, so the page can never send the
 * browser (or a code) anywhere the server did not name.
 */
export function ConnectPage() {
  const search = useSearch({ strict: false }) as ConnectSearch;
  const navigate = useNavigate();
  // Fixed on first render: the address is cleaned up below, and a reload reads the saved copy.
  const [request] = useState<ConnectRequest | null>(() => complete(search) ?? savedRequest());
  const [choice, setChoice] = useState<string | null>(null);

  useEffect(() => {
    if (request) saveRequest(request);
  }, [request]);

  const validated = useQuery({
    queryKey: ["@connect", "validate", request],
    queryFn: () => connectApi.validate(request!),
    enabled: request !== null,
    retry: false,
    staleTime: Infinity,
  });
  const v = validated.data;
  const projects = useQuery({
    queryKey: ["@connect", "projects"],
    queryFn: connectApi.projects,
    enabled: !!v,
    retry: false,
  });

  // The session check passed (validate answered): strip the query from the address.
  useEffect(() => {
    if (v && Object.keys(search).length > 0) void navigate({ to: "/connect", search: {}, replace: true });
  }, [v, search, navigate]);

  const list = projects.data ?? [];
  const keyOf = (org: string, slug: string) => `${org}/${slug}`;
  const hinted = v?.org && v.project ? keyOf(v.org, v.project) : null;
  const selectedKey =
    choice ??
    (hinted && list.some((p) => keyOf(p.org, p.slug) === hinted) ? hinted : null) ??
    (list.length === 1 ? keyOf(list[0].org, list[0].slug) : "");
  const selected = list.find((p) => keyOf(p.org, p.slug) === selectedKey);

  const allow = useMutation({
    mutationFn: () => connectApi.authorize({ ...request!, org: selected!.org, project: selected!.slug }),
    onSuccess: ({ redirect }) => {
      forgetRequest();
      void hardNavigate(redirect);
    },
  });
  const cancel = () => {
    forgetRequest();
    void hardNavigate(v!.cancel_url);
  };

  if (request === null) {
    return <Refused error={new ApiError(422, "The address is missing parts of the request.", "bad_connect_request")} />;
  }
  if (validated.isError) return <Refused error={validated.error} />;
  if (!v) {
    return (
      <AuthLayout title="Connect a local WhyGraph">
        <Skeleton className="h-24" />
      </AuthLayout>
    );
  }

  return (
    <AuthLayout
      title="Connect a local WhyGraph"
      description={
        <>
          <strong data-testid="connect-client">{v.client_name}</strong> on this computer (port{" "}
          <span className="font-mono">{v.port}</span>) asks to read a project's history on your behalf.
        </>
      }
    >
      {projects.isLoading && <Skeleton className="h-10" />}
      {projects.isError && (
        <Alert variant="destructive">
          <AlertDescription>{linkError(projects.error)}</AlertDescription>
        </Alert>
      )}
      {projects.isSuccess && list.length === 0 && (
        <Alert data-testid="connect-no-projects">
          <AlertTitle>No projects to connect</AlertTitle>
          <AlertDescription>
            You are not a member of any project yet. Ask an admin of your organization to add your
            GitHub username, then start again.
          </AlertDescription>
        </Alert>
      )}
      {list.length > 0 && (
        <Field label="Project" hint="Only projects of organizations you are a member of are listed.">
          {(p) => (
            <select
              {...p}
              className={nativeSelectClass}
              value={selectedKey}
              onChange={(e) => setChoice(e.target.value)}
            >
              <option value="">Choose a project…</option>
              {list.map((proj) => (
                <option key={keyOf(proj.org, proj.slug)} value={keyOf(proj.org, proj.slug)}>
                  {proj.org_name} / {proj.name}
                </option>
              ))}
            </select>
          )}
        </Field>
      )}
      {selected?.access_lost && (
        <Alert data-testid="connect-access-lost">
          <AlertTitle>WhyGraph cannot read this repository right now</AlertTitle>
          <AlertDescription>
            GitHub no longer lets WhyGraph read it. You can still connect; the history already scanned
            stays available.
          </AlertDescription>
        </Alert>
      )}
      {allow.isError && (
        <Alert variant="destructive" data-testid="connect-error">
          <AlertDescription>{linkError(allow.error)}</AlertDescription>
        </Alert>
      )}
      <p className="text-xs text-muted-foreground">
        Allowing gives that machine read-only access to this project until you revoke it in your account.
      </p>
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => allow.mutate()} disabled={!selected || allow.isPending}>
          {allow.isPending ? "Connecting…" : "Allow"}
        </Button>
        <Button variant="outline" onClick={cancel}>
          Cancel
        </Button>
      </div>
    </AuthLayout>
  );
}
