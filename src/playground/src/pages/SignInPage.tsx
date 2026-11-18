import { useSearch } from "@tanstack/react-router";
import { useMutation } from "@tanstack/react-query";
import { authApi } from "../api";
import { AuthLayout } from "../components/auth/AuthLayout";
import { GitHubMark } from "../components/auth/GitHubMark";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { authMessage } from "../lib/authErrors";
import { isSafeNext, usePortalState } from "../lib/identity";
import { hardNavigate } from "../lib/navigation";

/**
 * What the sign-in page says under its title (NAV-8, ER-7): a session that ended
 * (`reauth=1`: an org host sent the person back), else where a valid `next` lands
 * ("...to continue to acme.example.com/p/api", or connecting a local portal).
 */
export function signInDescription(next: string | undefined, reauth: boolean, baseUrl: string | undefined): string {
  if (reauth) return "Your session ended. Sign in to continue.";
  if (!next || !isSafeNext(next, baseUrl)) return "Sign in to WhyGraph.";
  const url = new URL(next);
  const base = new URL(baseUrl!);
  if (url.host === base.host && url.pathname.replace(/\/+$/, "") === "/connect") {
    return "Sign in to connect your local WhyGraph.";
  }
  const path = url.pathname === "/" ? "" : url.pathname.replace(/\/+$/, "");
  return `Sign in to continue to ${url.host}${path}.`;
}

/**
 * `<base>/signin`: GitHub sign-in (which also creates an account on first use).
 * `?next=` is where the server may send you back. Administrators sign in at
 * `/admin/login`.
 */
export function SignInPage() {
  const { next, reauth } = useSearch({ strict: false }) as { next?: string; reauth?: boolean };
  const baseUrl = usePortalState().data?.base_url;

  const start = useMutation({
    mutationFn: () => authApi.githubStart({ next }),
    onSuccess: ({ authorize_url }) => hardNavigate(authorize_url),
  });
  // A successful start leaves the page, so the button stays busy until it does.
  const busy = start.isPending || start.isSuccess;

  return (
    <AuthLayout title="Sign in" description={signInDescription(next, !!reauth, baseUrl)}>
      <Button size="lg" disabled={busy} onClick={() => start.mutate()}>
        <GitHubMark className="size-4" />
        {busy ? "Opening GitHub…" : "Sign in with GitHub"}
      </Button>
      <p className="text-center text-xs text-muted-foreground">
        New here? Signing in with GitHub creates your account.
      </p>
      {start.isError && (
        <Alert variant="destructive">
          <AlertDescription>{authMessage(start.error)}</AlertDescription>
        </Alert>
      )}
    </AuthLayout>
  );
}
