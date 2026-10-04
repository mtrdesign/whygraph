import { useEffect, useRef, useState } from "react";
import { Link, useSearch } from "@tanstack/react-router";
import { useMutation } from "@tanstack/react-query";
import { ApiError, authApi } from "../api";
import { AuthLayout } from "../components/auth/AuthLayout";
import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert";
import { authMessage } from "../lib/authErrors";
import { useFinishAuth } from "../lib/identity";

interface CallbackParams {
  code?: string;
  state?: string;
  error?: string;
}

/** GitHub's own `?error=` (no POST then): `access_denied` is the user's Cancel. */
function githubErrorMessage(error: string): string {
  return error === "access_denied"
    ? "Sign-in was cancelled."
    : "GitHub did not complete the sign-in. Try again.";
}

/**
 * `<base>/auth/github`: where GitHub sends the browser back. Reads `code` and
 * `state` (or GitHub's `error`) once, removes them from the address bar at once,
 * then posts them; the server sets the session and names where to go next.
 */
export function GitHubCallbackPage() {
  const search = useSearch({ strict: false }) as CallbackParams;
  const finish = useFinishAuth();
  const [params] = useState<CallbackParams>(() => ({
    code: search.code,
    state: search.state,
    error: search.error,
  }));
  const posted = useRef(false);

  const callback = useMutation({
    mutationFn: (body: { code: string; state: string }) => authApi.githubCallback(body),
    onSuccess: ({ redirect }) => finish(redirect),
  });

  useEffect(() => {
    if (window.location.search) {
      window.history.replaceState(window.history.state, "", window.location.pathname + window.location.hash);
    }
    // Once only: the code is single-use, and StrictMode runs effects twice.
    if (posted.current || params.error || !params.code || !params.state) return;
    posted.current = true;
    callback.mutate({ code: params.code, state: params.state });
  }, [params, callback]);

  let problem: { message: string; fixUrl?: string } | null = null;
  if (params.error) {
    problem = { message: githubErrorMessage(params.error) };
  } else if (!params.code || !params.state) {
    problem = { message: "This sign-in link is incomplete. Start again from the sign-in page." };
  } else if (callback.isError) {
    const err = callback.error;
    const fix = err instanceof ApiError && typeof err.extra.fix_url === "string" ? err.extra.fix_url : undefined;
    problem = { message: authMessage(err), fixUrl: fix };
  }

  if (!problem) {
    return (
      <AuthLayout title="Signing in">
        <p className="text-sm text-muted-foreground" data-testid="github-callback-pending">
          Finishing the sign-in with GitHub…
        </p>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout title="Sign-in did not complete">
      <Alert variant="destructive" data-testid="github-callback-error">
        <AlertTitle>{problem.message}</AlertTitle>
        {problem.fixUrl && (
          <AlertDescription>
            <a href={problem.fixUrl} className="text-primary-text underline" target="_blank" rel="noreferrer">
              Turn on two-factor authentication on GitHub
            </a>
            , then sign in again.
          </AlertDescription>
        )}
      </Alert>
      <Link to="/signin" className="text-sm text-primary-text hover:underline">
        Back to sign in
      </Link>
    </AuthLayout>
  );
}
