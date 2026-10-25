import { errorInfo } from "../lib/apiErrors";
import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ApiError, connectApi, type ConnectRequest } from "../api";
import { AuthLayout } from "../components/auth/AuthLayout";
import { Field } from "../components/portal/Field";
import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../components/ui/select";
import { Skeleton } from "../components/ui/skeleton";
import { linkError } from "../lib/errors";
import { usePortalState } from "../lib/identity";
import { hardNavigate } from "../lib/navigation";

/** The consent query's keys: what `/connect?...` carries from the local portal. */
export type ConnectSearch = Partial<Record<keyof ConnectRequest, string>>;

const SAVED_KEY = "whygraph.connect";
// Set when a consent finished here, so Back to this page says so instead of "refused".
const DONE_KEY = "whygraph.connect.done";

// The query survives the GitHub sign-in round trip in `sessionStorage` (best
// effort: storage may be blocked), and is stripped from the address only once the
// server has validated it for a signed-in session.
function saveRequest(req: ConnectRequest): void {
  try {
    window.sessionStorage.setItem(SAVED_KEY, JSON.stringify(req));
    window.sessionStorage.removeItem(DONE_KEY); // a new request: no longer finished
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

function markDone(): void {
  try {
    window.sessionStorage.setItem(DONE_KEY, "1");
  } catch {
    // Back then shows the refused page, as before
  }
}

function isDone(): boolean {
  try {
    return window.sessionStorage.getItem(DONE_KEY) === "1";
  } catch {
    return false;
  }
}

/** The request when every required field is present, else `null` (the server judges validity). */
export function complete(q: ConnectSearch): ConnectRequest | null {
  const { redirect_uri, code_challenge, code_challenge_method, state, client_name } = q;
  if (!redirect_uri || !code_challenge || !code_challenge_method || !state || !client_name) return null;
  const req: ConnectRequest = { redirect_uri, code_challenge, code_challenge_method, state, client_name };
  if (q.org) req.org = q.org;
  if (q.project) req.project = q.project;
  if (q.intent) req.intent = q.intent;
  return req;
}

function Refused({ error }: { error: unknown }) {
  // The server's words; for a refused request there is no URL to go back to.
  const message =
    error instanceof ApiError && error.code === "bad_connect_request"
      ? [linkError(error), errorInfo(error, "link").detail].filter(Boolean).join(" ")
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
 *
 * When the platform's own card started the flow, the request carries a one-time
 * intent and validate answers `pre_approved`: the page then submits once by itself
 * ("Connecting ...") instead of showing Allow. Anything short of that (an expired
 * intent, another project, a failed list) falls back to the normal page, and the
 * manual Allow never sends the intent.
 */
export function ConnectPage() {
  const search = useSearch({ strict: false }) as ConnectSearch;
  const navigate = useNavigate();
  // Fixed on first render: the address is cleaned up below, and a reload reads the saved copy.
  const [request] = useState<ConnectRequest | null>(() => complete(search) ?? savedRequest());
  const [choice, setChoice] = useState<string | null>(null);
  const login = usePortalState().data?.user?.github_login;
  // Set once the auto path is abandoned; `intentExpired` when the server refused the intent.
  const [dropped, setDropped] = useState(false);
  const [intentExpired, setIntentExpired] = useState(false);
  const sent = useRef(false);
  const allowRef = useRef<HTMLButtonElement>(null);
  // Fixed on first render, like `request`: Back after a finished consent reads it.
  const [done] = useState(() => request === null && isDone());

  useEffect(() => {
    if (request) saveRequest(request);
  }, [request]);

  // A page restored from the back-forward cache is frozen on "Connecting...": reload it.
  useEffect(() => {
    const onShow = (e: PageTransitionEvent) => {
      if (e.persisted) window.location.reload();
    };
    window.addEventListener("pageshow", onShow);
    return () => window.removeEventListener("pageshow", onShow);
  }, []);

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

  // Only the auto path sends the intent; the Allow button calls `allow.mutate(false)`.
  const allow = useMutation({
    mutationFn: (withIntent: boolean) => {
      const { intent: carried, ...base } = request!;
      return connectApi.authorize({
        ...base,
        ...(withIntent && carried ? { intent: carried } : {}),
        org: selected!.org,
        project: selected!.slug,
      });
    },
    onSuccess: ({ redirect }) => {
      forgetRequest();
      markDone();
      void hardNavigate(redirect);
    },
  });

  const preApproved = !!v?.pre_approved && !dropped;
  const autoOk = preApproved && hinted !== null && choice === null && projects.isSuccess && selectedKey === hinted;
  // `sent` is a ref, so StrictMode's double effect never submits twice. Keyed on
  // `autoOk` alone: `allow` is a new object every render.
  useEffect(() => {
    if (!autoOk || sent.current) return;
    sent.current = true;
    allow.mutate(true, {
      onError: (err) => {
        setDropped(true);
        if (err instanceof ApiError && err.code === "intent_expired") {
          setIntentExpired(true);
          allow.reset();
        }
      },
    });
  }, [autoOk]);
  useEffect(() => {
    if (preApproved && (projects.isError || (projects.isSuccess && selectedKey !== hinted))) setDropped(true);
  }, [preApproved, projects.isError, projects.isSuccess, selectedKey, hinted]);
  // Back on the normal page after a refused intent: the next step is Allow.
  useEffect(() => {
    if (intentExpired) allowRef.current?.focus();
  }, [intentExpired]);
  const cancel = () => {
    forgetRequest();
    void hardNavigate(v!.cancel_url);
  };

  if (request === null) {
    if (done) {
      return (
        <AuthLayout title="Connection finished">
          <p className="text-sm text-muted-foreground" data-testid="connect-done">
            You can close this tab.
          </p>
        </AuthLayout>
      );
    }
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
  if (preApproved && (projects.isLoading || autoOk || allow.isPending || allow.isSuccess)) {
    const orgName = selected?.org_name ?? v.orgs.find((o) => o.slug === v.org)?.name;
    const place = [orgName, selected?.name].filter(Boolean).join(" / ");
    const who = [place && `to ${place}`, login && `as @${login}`].filter(Boolean).join(" ");
    return (
      <AuthLayout title={`Connecting ${v.client_name}…`}>
        <div role="status" aria-busy="true" data-testid="connect-auto" className="flex flex-col gap-3">
          {who && <p className="text-sm text-muted-foreground">{who}</p>}
          <Skeleton className="h-10" />
        </div>
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
          {(p) => {
            const items = list.map((proj) => ({
              value: keyOf(proj.org, proj.slug),
              label: `${proj.org_name} / ${proj.name}`,
            }));
            return (
              <Select
                items={items}
                value={selectedKey || null}
                onValueChange={(next) => {
                  if (typeof next === "string") setChoice(next);
                }}
              >
                <SelectTrigger {...p} className="w-full min-w-0">
                  <SelectValue placeholder="Choose a project…" />
                </SelectTrigger>
                <SelectContent alignItemWithTrigger={false}>
                  {items.map((item) => (
                    <SelectItem key={item.value} value={item.value}>
                      {item.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            );
          }}
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
      {intentExpired && (
        <Alert data-testid="connect-intent-expired">
          <AlertDescription>
            This one-time approval expired or was already used. Approve this connection below.
          </AlertDescription>
        </Alert>
      )}
      {allow.isError && (
        <Alert variant="destructive" data-testid="connect-error">
          <AlertDescription>{linkError(allow.error)}</AlertDescription>
        </Alert>
      )}
      <p className="text-xs text-muted-foreground">
        Allowing lets that machine read this project's history as you and, if your role allows, ask for
        rationale cards, which use the organization's AI keys within its hourly limit. You can revoke it
        any time on your Account page, under Connected machines.
      </p>
      <div className="flex flex-wrap gap-2">
        <Button ref={allowRef} onClick={() => allow.mutate(false)} disabled={!selected || allow.isPending}>
          {allow.isPending ? "Connecting…" : "Allow"}
        </Button>
        <Button variant="outline" onClick={cancel}>
          Cancel
        </Button>
      </div>
    </AuthLayout>
  );
}
