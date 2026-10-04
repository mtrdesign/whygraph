import { useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { ApiError, authApi } from "../api";
import { AuthLayout } from "../components/auth/AuthLayout";
import { Field } from "../components/portal/Field";
import { Alert, AlertDescription } from "../components/ui/alert";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { PASSWORD_HINT, authMessage } from "../lib/authErrors";
import { useFinishAuth } from "../lib/identity";

const FIELD_CODES = new Set(["bad_secret", "weak_password", "common_password", "bad_email", "email_taken"]);

/**
 * First run in production, on the base host: claim the instance with the secret
 * the portal printed in its log. The first account becomes the instance admin.
 */
export function BootstrapPage() {
  const finish = useFinishAuth();
  const [secret, setSecret] = useState("");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const bootstrap = useMutation({
    mutationFn: () => authApi.bootstrap({ secret: secret.trim(), email, display_name: name.trim(), password }),
    onSuccess: ({ redirect }) => finish(redirect),
  });
  const code = bootstrap.error instanceof ApiError ? bootstrap.error.code : undefined;
  const fieldError = (...codes: string[]) =>
    code && codes.includes(code) ? authMessage(bootstrap.error) : undefined;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    bootstrap.mutate();
  };

  return (
    <AuthLayout
      title="Set up WhyGraph"
      description="Create the first administrator. The portal printed a bootstrap secret in its log when it started."
    >
      <form noValidate className="flex flex-col gap-4" onSubmit={submit}>
        <Field
          label="Bootstrap secret"
          hint={
            <>
              Look for the log line <span className="font-mono">Bootstrap secret: ...</span>.
            </>
          }
          error={fieldError("bad_secret")}
        >
          {(p) => (
            <Input
              {...p}
              autoFocus
              autoComplete="off"
              spellCheck={false}
              className="font-mono"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
            />
          )}
        </Field>
        <Field label="Your name">
          {(p) => <Input {...p} autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} />}
        </Field>
        <Field label="Email" error={fieldError("bad_email", "email_taken")}>
          {(p) => (
            <Input
              {...p}
              type="email"
              autoComplete="username"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          )}
        </Field>
        <Field
          label="Password"
          hint={PASSWORD_HINT}
          error={fieldError("weak_password", "common_password")}
        >
          {(p) => (
            <Input
              {...p}
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          )}
        </Field>
        {bootstrap.isError && !(code && FIELD_CODES.has(code)) && (
          <Alert variant="destructive">
            <AlertDescription>{authMessage(bootstrap.error)}</AlertDescription>
          </Alert>
        )}
        <Button
          type="submit"
          disabled={bootstrap.isPending || !secret.trim() || !name.trim() || !email || !password}
        >
          {bootstrap.isPending ? "Creating…" : "Create administrator"}
        </Button>
      </form>
    </AuthLayout>
  );
}
