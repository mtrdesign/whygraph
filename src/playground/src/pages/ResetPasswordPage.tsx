import { useEffect, useState, type FormEvent } from "react";
import { Link } from "@tanstack/react-router";
import { useMutation } from "@tanstack/react-query";
import { ApiError, authApi } from "../api";
import { AuthLayout } from "../components/auth/AuthLayout";
import { Field } from "../components/portal/Field";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { PASSWORD_HINT, authMessage } from "../lib/authErrors";
import { useFinishAuth } from "../lib/identity";

/** The one-time token in `#token=...`, which never reaches a server log or `Referer`. */
function tokenFromHash(): string {
  return new URLSearchParams(window.location.hash.replace(/^#/, "")).get("token") ?? "";
}

/**
 * `<base>/reset#token=...`: choose a new password from an admin-issued link. The
 * token is read once and the fragment is removed from the address bar at once.
 */
export function ResetPasswordPage() {
  const finish = useFinishAuth();
  const [token] = useState(tokenFromHash);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");

  useEffect(() => {
    if (window.location.hash) {
      window.history.replaceState(window.history.state, "", window.location.pathname + window.location.search);
    }
  }, []);

  const reset = useMutation({
    mutationFn: () => authApi.reset({ token, password }),
    onSuccess: ({ redirect }) => finish(redirect),
  });
  const code = reset.error instanceof ApiError ? reset.error.code : undefined;
  const mismatch = confirm !== "" && confirm !== password ? "The passwords do not match." : undefined;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    reset.mutate();
  };

  if (!token) {
    return (
      <AuthLayout title="Reset your password">
        <Alert variant="destructive">
          <AlertDescription>
            This reset link is incomplete. Ask an instance administrator for a new one.
          </AlertDescription>
        </Alert>
        <Link to="/signin" className="text-sm text-primary-text hover:underline">
          Back to sign in
        </Link>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout title="Reset your password" description="Choose a new password. You will be signed in.">
      <form noValidate className="flex flex-col gap-4" onSubmit={submit}>
        <Field
          label="New password"
          hint={PASSWORD_HINT}
          error={code === "weak_password" || code === "common_password" ? authMessage(reset.error) : undefined}
        >
          {(p) => (
            <Input
              {...p}
              type="password"
              autoFocus
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          )}
        </Field>
        <Field label="Confirm new password" error={mismatch}>
          {(p) => (
            <Input
              {...p}
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
            />
          )}
        </Field>
        {reset.isError && code !== "weak_password" && code !== "common_password" && (
          <Alert variant="destructive">
            <AlertDescription>{authMessage(reset.error)}</AlertDescription>
          </Alert>
        )}
        <Button type="submit" disabled={reset.isPending || !password || password !== confirm}>
          {reset.isPending ? "Saving…" : "Set password"}
        </Button>
      </form>
    </AuthLayout>
  );
}
