import { useState, type FormEvent } from "react";
import { useSearch } from "@tanstack/react-router";
import { useMutation } from "@tanstack/react-query";
import { ChevronDownIcon, ChevronRightIcon } from "lucide-react";
import { authApi } from "../api";
import { AuthLayout } from "../components/auth/AuthLayout";
import { GitHubMark } from "../components/auth/GitHubMark";
import { Field } from "../components/portal/Field";
import { AnnouncementBanner } from "../components/shell/AnnouncementBanner";
import { LiveRegion } from "../components/shell/LiveRegion";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { authMessage } from "../lib/authErrors";
import { isSafeNext, useFinishAuth, usePortalState } from "../lib/identity";
import { hardNavigate } from "../lib/navigation";
import { useDocumentTitle } from "../lib/useDocumentTitle";

/**
 * Where the admin sign-in returns to: the `next` the 401 redirect carried when it is
 * an absolute base-host address, else the dashboard. The server checks it again
 * (`safe_admin_redirect`), and it must be absolute (`${base_url}/admin...`).
 */
function adminNext(next: string | undefined, baseUrl: string | undefined): string | undefined {
  if (next && isSafeNext(next, baseUrl)) return next;
  return baseUrl ? `${baseUrl.replace(/\/+$/, "")}/admin` : undefined;
}

/** The administrator's email and password, behind the disclosure. */
function PasswordForm({ next }: { next?: string }) {
  const finish = useFinishAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const login = useMutation({
    mutationFn: () => authApi.adminLogin({ email, password, next }),
    onSuccess: ({ redirect }) => finish(redirect),
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    login.mutate();
  };
  return (
    <form noValidate id="admin-sign-in" className="flex flex-col gap-4" onSubmit={submit}>
      <Field label="Email">
        {(p) => (
          <Input
            {...p}
            type="email"
            autoFocus
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        )}
      </Field>
      <Field label="Password">
        {(p) => (
          <Input
            {...p}
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        )}
      </Field>
      {login.isError && (
        <Alert variant="destructive">
          <AlertDescription>{authMessage(login.error)}</AlertDescription>
        </Alert>
      )}
      <Button type="submit" variant="outline" disabled={login.isPending || !email || !password}>
        {login.isPending ? "Signing in…" : "Sign in"}
      </Button>
      <p className="text-xs text-muted-foreground">
        Forgot your password? Ask another platform administrator for a reset link.
      </p>
    </form>
  );
}

/**
 * `<base>/admin/login`: platform administration's own sign-in. GitHub is the main
 * way in; the bootstrap password account is behind a disclosure. It calls no
 * `/api/admin/*` route. "Your admin session ended" shows only with `reauth=1`:
 * `next` alone is on every first visit, because the Administration link leads to
 * `/admin`, which answers 401 and redirects here.
 */
export function AdminLoginPage() {
  const { next, reauth } = useSearch({ strict: false }) as { next?: string; reauth?: boolean };
  const baseUrl = usePortalState().data?.base_url;
  const [passwordOpen, setPasswordOpen] = useState(false);
  useDocumentTitle("Admin sign-in");
  const target = adminNext(next, baseUrl);

  const start = useMutation({
    mutationFn: () => authApi.githubAdminStart({ next: target }),
    onSuccess: ({ authorize_url }) => hardNavigate(authorize_url),
  });
  // A successful start leaves the page, so the button stays busy until it does.
  const busy = start.isPending || start.isSuccess;

  return (
    <>
      <AnnouncementBanner />
      <main id="main" tabIndex={-1} className="outline-none">
      <AuthLayout
        title="Sign in to administration"
        description={
          <>
            For platform administrators. Use the GitHub account that was made an administrator.
            {reauth && <span className="mt-1 block">Your admin session ended. Sign in again to continue.</span>}
          </>
        }
      >
        <Button size="lg" disabled={busy} onClick={() => start.mutate()}>
          <GitHubMark className="size-4" />
          {busy ? "Opening GitHub…" : "Continue with GitHub"}
        </Button>
        {start.isError && (
          <Alert variant="destructive">
            <AlertDescription>{authMessage(start.error)}</AlertDescription>
          </Alert>
        )}
        <div className="border-t border-border pt-3">
          <button
            type="button"
            aria-expanded={passwordOpen}
            aria-controls="admin-sign-in"
            onClick={() => setPasswordOpen((o) => !o)}
            className="flex items-center gap-1 rounded-sm text-[13px] text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
          >
            {passwordOpen ? <ChevronDownIcon className="size-3.5" /> : <ChevronRightIcon className="size-3.5" />}
            Sign in with a password
          </button>
          {passwordOpen && (
            <div className="mt-3">
              <PasswordForm next={target} />
            </div>
          )}
        </div>
      </AuthLayout>
      </main>
      <LiveRegion />
    </>
  );
}
