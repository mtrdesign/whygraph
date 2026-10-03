import { useState, type FormEvent } from "react";
import { Link, useSearch } from "@tanstack/react-router";
import { useMutation } from "@tanstack/react-query";
import { authApi } from "../api";
import { AuthLayout } from "../components/auth/AuthLayout";
import { Field } from "../components/portal/Field";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { authMessage } from "../lib/authErrors";
import { useFinishAuth } from "../lib/identity";

/** `<base>/signin`: email and password; `?next=` is where the server may send you back. */
export function SignInPage() {
  const { next } = useSearch({ strict: false }) as { next?: string };
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
    <AuthLayout
      title="Sign in"
      description="Sign in to WhyGraph."
      footer={
        <>
          No account yet?{" "}
          <Link to="/register" className="text-primary-text hover:underline">
            Create one
          </Link>
          . Forgot your password? Ask an instance administrator for a reset link.
        </>
      }
    >
      <form noValidate className="flex flex-col gap-4" onSubmit={submit}>
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
        <Button type="submit" disabled={login.isPending || !email || !password}>
          {login.isPending ? "Signing in…" : "Sign in"}
        </Button>
      </form>
    </AuthLayout>
  );
}
