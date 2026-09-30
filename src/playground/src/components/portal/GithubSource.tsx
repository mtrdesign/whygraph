import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { useMutation } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { portalApi, type AddProjectResult } from "../../api";
import { addProjectError, type AddError } from "../../lib/errors";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Field } from "./Field";

const schema = z.object({
  url: z
    .string()
    .trim()
    .regex(
      /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+?(\.git)?\/?$/,
      "Enter a repository URL like https://github.com/owner/repo",
    ),
  token: z.string().trim().min(1, "A token is required to clone"),
});
type Values = z.infer<typeof schema>;

/**
 * Screen 4: clone a GitHub repository into the portal's own data folder. The
 * backend probes access before cloning, and answers with a code per failure
 * (`bad_token`, `no_access`, `not_found`, ...) that `addProjectError` maps onto
 * the field that needs fixing.
 */
export function GithubSource({ onAdded }: { onAdded: (result: AddProjectResult) => void }) {
  const [serverError, setServerError] = useState<AddError | null>(null);
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { url: "", token: "" },
  });

  const add = useMutation({
    mutationFn: (v: Values) => portalApi.addProject({ source: "github", url: v.url, token: v.token }),
    onSuccess: onAdded,
    onError: (err) => setServerError(addProjectError(err, "github")),
  });

  const { errors } = form.formState;
  const serverFor = (field: "url" | "token") =>
    serverError?.field === field ? serverError.message : undefined;

  return (
    <form
      className="flex flex-col gap-4 p-5"
      noValidate
      onSubmit={form.handleSubmit((v) => {
        setServerError(null);
        add.mutate(v);
      })}
    >
      <Field
        label="Repository URL"
        error={errors.url?.message ?? serverFor("url")}
        hint="The portal clones it into its own data folder; your other folders are not touched."
      >
        {(p) => (
          <Input
            {...p}
            placeholder="https://github.com/owner/repo"
            autoComplete="off"
            {...form.register("url", { onChange: () => setServerError(null) })}
          />
        )}
      </Field>

      <Field
        label="Access token"
        error={errors.token?.message ?? serverFor("token")}
        hint="A fine-grained or classic token with read access to the repository's contents, plus pull requests and issues for the GitHub crawl. Stored encrypted; never shown again."
      >
        {(p) => (
          <Input
            {...p}
            type="password"
            autoComplete="off"
            placeholder="github_pat_…"
            {...form.register("token", { onChange: () => setServerError(null) })}
          />
        )}
      </Field>

      {serverError && serverError.field === "form" && (
        <Alert variant="destructive">
          <AlertTitle>Could not add the project</AlertTitle>
          <AlertDescription>{serverError.message}</AlertDescription>
        </Alert>
      )}

      <div className="flex items-center justify-end gap-2 pt-1">
        <Button type="button" variant="ghost" render={<Link to="/" />}>
          Cancel
        </Button>
        <Button type="submit" disabled={add.isPending}>
          {add.isPending ? "Cloning…" : "Add project"}
        </Button>
      </div>
    </form>
  );
}
