import { useState, type FormEvent } from "react";
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

const FIELD_CODES = new Set(["weak_password", "common_password", "bad_email", "email_taken"]);

/** `<base>/register`: open registration. A new account lands on the org picker. */
export function RegisterPage() {
  const finish = useFinishAuth();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  const register = useMutation({
    mutationFn: () => authApi.register({ email, display_name: name.trim(), password }),
    onSuccess: ({ redirect }) => finish(redirect),
  });
  const code = register.error instanceof ApiError ? register.error.code : undefined;
  const fieldError = (...codes: string[]) =>
    code && codes.includes(code) ? authMessage(register.error) : undefined;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    register.mutate();
  };

  return (
    <AuthLayout
      title="Create your account"
      footer={
        <>
          Already have an account?{" "}
          <Link to="/signin" className="text-primary-text hover:underline">
            Sign in
          </Link>
        </>
      }
    >
      <form noValidate className="flex flex-col gap-4" onSubmit={submit}>
        <Field label="Your name">
          {(p) => (
            <Input {...p} autoFocus autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} />
          )}
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
        {register.isError && !(code && FIELD_CODES.has(code)) && (
          <Alert variant="destructive">
            <AlertDescription>{authMessage(register.error)}</AlertDescription>
          </Alert>
        )}
        <Button type="submit" disabled={register.isPending || !name.trim() || !email || !password}>
          {register.isPending ? "Creating…" : "Create account"}
        </Button>
      </form>
    </AuthLayout>
  );
}
