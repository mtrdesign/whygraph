import { useState, type FormEvent } from "react";
import { useSearch } from "@tanstack/react-router";
import { useMutation } from "@tanstack/react-query";
import { ChevronDownIcon, ChevronRightIcon } from "lucide-react";
import { authApi } from "../api";
import { AuthLayout } from "../components/auth/AuthLayout";
import { GitHubMark } from "../components/auth/GitHubMark";
import { Field } from "../components/portal/Field";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { authMessage } from "../lib/authErrors";
import { useFinishAuth } from "../lib/identity";
import { hardNavigate } from "../lib/navigation";

/** The instance administrator's email and password, behind the disclosure. */
function AdminSignIn({ next }: { next?: string }) {
  const finish = useFinishAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const login = useMutation({
    mutationFn: () => authApi.login({ email, password, next }),
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
        Forgot your password? Ask an instance administrator for a reset link.
      </p>
    </form>
  );
}

/**
 * `<base>/signin`: GitHub sign-in (which also creates an account on first use),
 * with the administrator's password form behind a disclosure. `?next=` is where
 * the server may send you back.
 */
export function SignInPage() {
  const { next } = useSearch({ strict: false }) as { next?: string };
  const [adminOpen, setAdminOpen] = useState(false);

  const start = useMutation({
    mutationFn: () => authApi.githubStart({ next }),
    onSuccess: ({ authorize_url }) => hardNavigate(authorize_url),
  });
  // A successful start leaves the page, so the button stays busy until it does.
  const busy = start.isPending || start.isSuccess;

  return (
    <AuthLayout title="Sign in" description="Sign in to WhyGraph.">
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
      <div className="border-t border-border pt-3">
        <button
          type="button"
          aria-expanded={adminOpen}
          aria-controls="admin-sign-in"
          onClick={() => setAdminOpen((o) => !o)}
          className="flex items-center gap-1 rounded-sm text-[13px] text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        >
          {adminOpen ? <ChevronDownIcon className="size-3.5" /> : <ChevronRightIcon className="size-3.5" />}
          Administrator sign-in
        </button>
        {adminOpen && (
          <div className="mt-3">
            <AdminSignIn next={next} />
          </div>
        )}
      </div>
    </AuthLayout>
  );
}
