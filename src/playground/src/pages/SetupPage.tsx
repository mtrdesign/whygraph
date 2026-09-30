import { useNavigate } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { portalApi, portalKey } from "../api";
import { Field } from "../components/portal/Field";
import { Alert, AlertDescription, AlertTitle } from "../components/ui/alert";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";

const schema = z.object({
  display_name: z.string().trim().min(1, "Enter a name").max(100, "At most 100 characters"),
});
type Values = z.infer<typeof schema>;

/**
 * Screen 1, first run only: create the one local user. The mode is fixed and
 * shown read-only; the router redirects here until `setup_complete`, and away
 * from here once it is.
 */
export function SetupPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  // The router's gate already fetched the state, so this is a cache hit.
  const state = useQuery({ queryKey: portalKey("state"), queryFn: portalApi.state });
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { display_name: "" } });

  const setup = useMutation({
    mutationFn: (v: Values) => portalApi.setup(v.display_name),
    onSuccess: async () => {
      // The route gate reads `setup_complete` from this query; refetch it before
      // leaving or it would bounce straight back to /setup.
      await queryClient.invalidateQueries({ queryKey: portalKey("state") });
      await navigate({ to: "/", replace: true });
    },
  });

  const shared = state.data?.shared_folders ?? [];
  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-6">
      <div className="flex w-full max-w-md flex-col gap-6">
        <div className="flex flex-col gap-1.5">
          <h1 className="text-2xl font-semibold tracking-tight">Welcome to WhyGraph</h1>
          <p className="text-sm text-muted-foreground">
            The portal keeps the history behind your code - commits, pull requests, issues and the
            reasoning they hold - and serves it to you and your coding agents.
          </p>
        </div>

        <form
          noValidate
          className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5"
          onSubmit={form.handleSubmit((v) => setup.mutate(v))}
        >
          <Field label="Your name" error={form.formState.errors.display_name?.message}>
            {(p) => (
              <Input {...p} autoFocus autoComplete="name" placeholder="Ada Lovelace" {...form.register("display_name")} />
            )}
          </Field>

          <div className="flex items-center justify-between text-sm">
            <span className="text-muted-foreground">Mode</span>
            <Badge variant="secondary">Local</Badge>
          </div>
          {shared.length > 0 && (
            <div className="flex flex-col gap-1 text-sm">
              <span className="text-muted-foreground">Shared folders</span>
              <ul className="font-mono text-xs">
                {shared.map((f) => (
                  <li key={f}>{f}</li>
                ))}
              </ul>
            </div>
          )}

          {setup.isError && (
            <Alert variant="destructive">
              <AlertTitle>Setup failed</AlertTitle>
              <AlertDescription>{setup.error.message}</AlertDescription>
            </Alert>
          )}

          <Button type="submit" disabled={setup.isPending}>
            {setup.isPending ? "Setting up…" : "Continue"}
          </Button>
        </form>

        <p className="text-xs text-muted-foreground">
          Local mode is for a machine only you use: anyone who can reach this port can read your
          projects and use your API keys, so do not expose it on a shared or public network.
        </p>
      </div>
    </div>
  );
}
