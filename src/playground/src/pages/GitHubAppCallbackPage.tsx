import { useEffect, useRef, useState } from "react";
import { Link, useSearch } from "@tanstack/react-router";
import { useMutation } from "@tanstack/react-query";
import { ApiError, githubApi, type GitHubAppCallbackBody } from "../api";
import { AuthLayout } from "../components/auth/AuthLayout";
import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert";
import { authMessage } from "../lib/authErrors";
import { hardNavigate } from "../lib/navigation";

/** The query GitHub sends back to `/auth/github-app`, as the router validated it. */
export interface GitHubAppCallbackParams {
  code?: string;
  state?: string;
  installation_id?: string;
  setup_action?: string;
  iss?: string;
  error?: string;
}

const SETUP_ACTIONS = ["install", "update", "request"] as const;

/** The POST body: only what GitHub sent, `installation_id` as a number, a known `setup_action`. */
export function callbackBody(p: GitHubAppCallbackParams): GitHubAppCallbackBody {
  const body: GitHubAppCallbackBody = {};
  if (p.code) body.code = p.code;
  if (p.state) body.state = p.state;
  if (p.iss) body.iss = p.iss;
  if (p.installation_id && /^\d+$/.test(p.installation_id)) body.installation_id = Number(p.installation_id);
  const action = SETUP_ACTIONS.find((a) => a === p.setup_action);
  if (action) body.setup_action = action;
  return body;
}

/** A refused callback, in this page's words where they differ from the sign-in's. */
function problemMessage(err: unknown): string {
  if (err instanceof ApiError && err.code === "oauth_state") {
    return "This authorization expired or was started in another browser. Start again from your organization's import page.";
  }
  if (err instanceof ApiError && err.code === "github_auth_failed") {
    return "GitHub did not confirm the authorization. Start again from your organization's import page.";
  }
  return authMessage(err);
}

/**
 * `<base>/auth/github-app`: where GitHub sends the browser back after a GitHub
 * App user authorization or an install, both started from an org's import page
 * (M2d-2 plan section 4.4). Reads the query once, removes it from the address
 * bar, posts it; the server keeps the user token for this session and names the
 * import page to return to. A `setup_action=request` (an org member asked the
 * GitHub organization's owners to install the app) only says so.
 */
export function GitHubAppCallbackPage() {
  const search = useSearch({ strict: false }) as GitHubAppCallbackParams;
  const [params] = useState<GitHubAppCallbackParams>(() => ({ ...search }));
  const posted = useRef(false);
  const request = params.setup_action === "request";
  const postable = !params.error && (!!params.code || !!params.setup_action);

  const callback = useMutation({
    mutationFn: (body: GitHubAppCallbackBody) => githubApi.callback(body),
    onSuccess: (r) => {
      if (!r.requested && r.return_to) void hardNavigate(r.return_to);
    },
  });

  useEffect(() => {
    if (window.location.search) {
      window.history.replaceState(window.history.state, "", window.location.pathname + window.location.hash);
    }
    // Once only: the code is single-use, and StrictMode runs effects twice.
    if (posted.current || !postable) return;
    posted.current = true;
    callback.mutate(callbackBody(params));
  }, [params, postable, callback]);

  const back = (
    <Link to="/orgs" className="text-sm text-primary-text hover:underline">
      Back to your organizations
    </Link>
  );

  if (request && callback.isSuccess) {
    const returnTo = callback.data.return_to;
    return (
      <AuthLayout title="Request sent">
        <Alert data-testid="github-app-callback-requested">
          <AlertTitle>Your request was sent to the GitHub organization's owners.</AlertTitle>
          <AlertDescription>
            Once one of them installs the WhyGraph app, its repositories appear on the import page.
          </AlertDescription>
        </Alert>
        {returnTo ? (
          <a href={returnTo} className="text-sm text-primary-text hover:underline">
            Back to the import page
          </a>
        ) : (
          back
        )}
      </AuthLayout>
    );
  }

  let problem: string | null = null;
  if (params.error) {
    problem =
      params.error === "access_denied"
        ? "The GitHub authorization was cancelled."
        : "GitHub did not complete the authorization. Try again.";
  } else if (!postable) {
    problem = "This return address is incomplete. Start again from your organization's import page.";
  } else if (callback.isError) {
    problem = problemMessage(callback.error);
  }

  if (!problem) {
    return (
      <AuthLayout title="Connecting GitHub">
        <p className="text-sm text-muted-foreground" data-testid="github-app-callback-pending">
          Finishing the authorization with GitHub…
        </p>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout title="GitHub was not connected">
      <Alert variant="destructive" data-testid="github-app-callback-error">
        <AlertTitle>{problem}</AlertTitle>
      </Alert>
      {back}
    </AuthLayout>
  );
}
