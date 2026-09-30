import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Controller, useForm, type FieldPath } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  ApiError,
  portalApi,
  portalKey,
  projectApi,
  projectKey,
  type ConfigPut,
  type ProjectConfigView,
  type SecretStatus,
} from "../../api";
import {
  CHAT_PROVIDER_TAGS,
  HOOK_NAMES,
  KEYED_PROVIDERS,
  PROVIDERS,
  TASKS,
  configFormSchema,
  layerToValues,
  secretsPatch,
  valuesToLayer,
  type ConfigFormValues,
} from "../../lib/configForm";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Badge } from "../ui/badge";
import { Button } from "../ui/button";
import { Checkbox } from "../ui/checkbox";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { Switch } from "../ui/switch";
import { Field, nativeSelectClass } from "./Field";

/**
 * Where the form reads and writes. `project` edits one project's layer (models,
 * keys, endpoints, GitHub, hooks); `global` edits the defaults every project
 * inherits (models, keys, endpoints only - rule 6 of §4.2.1).
 */
// The project config and the defaults differ only in `import` (project) and
// `no_provider_key` (defaults).
type StoredConfig = Omit<ProjectConfigView, "import"> & {
  import?: ProjectConfigView["import"];
  no_provider_key?: boolean;
};

export type ConfigScope = { kind: "project"; slug: string } | { kind: "global" };

function Section({
  title,
  description,
  children,
}: {
  title: string;
  description?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="flex flex-col gap-4 rounded-xl border border-border bg-card p-5">
      <div>
        <h2 className="text-sm font-semibold">{title}</h2>
        {description && <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>}
      </div>
      {children}
    </section>
  );
}

function ModelRow({
  name,
  label,
  hint,
  providers,
  emptyLabel,
  register,
  error,
}: {
  name: "defaultModel" | "analyze" | "rationale" | "chat";
  label: string;
  hint: string;
  providers: string[];
  emptyLabel: string;
  register: ReturnType<typeof useForm<ConfigFormValues>>["register"];
  error?: string;
}) {
  return (
    <Field label={label} hint={hint} error={error}>
      {(p) => (
        <div className="grid grid-cols-[minmax(0,11rem)_1fr] gap-2">
          <select
            aria-label={`${label} provider`}
            className={nativeSelectClass}
            {...register(`${name}.provider` as FieldPath<ConfigFormValues>)}
          >
            <option value="">{emptyLabel}</option>
            {providers.map((prov) => (
              <option key={prov} value={prov}>
                {prov}
              </option>
            ))}
          </select>
          <Input
            {...p}
            aria-label={`${label} model`}
            placeholder="model id (blank = provider default)"
            className="font-mono"
            {...register(`${name}.model` as FieldPath<ConfigFormValues>)}
          />
        </div>
      )}
    </Field>
  );
}

function KeyRow({
  provider,
  status,
  inherited,
  missing,
  pendingRemoval,
  onRemove,
  onUndo,
  inputProps,
}: {
  provider: string;
  status: SecretStatus | undefined;
  /** The global key that applies when this scope has none (project scope only). */
  inherited: SecretStatus | undefined;
  missing: boolean;
  pendingRemoval: boolean;
  onRemove: () => void;
  onUndo: () => void;
  inputProps: React.ComponentProps<"input">;
}) {
  return (
    <div className="flex flex-col gap-1.5" data-testid={`key-${provider}`}>
      <div className="flex flex-wrap items-center gap-2">
        <Label htmlFor={`key-${provider}`} className="w-28 font-mono text-[13px]">
          {provider}
        </Label>
        {status?.set ? (
          <Badge variant="secondary">
            set {status.hint}
            {status.unreadable ? " - unreadable, re-enter it" : ""}
          </Badge>
        ) : inherited?.set ? (
          <Badge variant="outline">using the global key {inherited.hint}</Badge>
        ) : (
          <Badge variant="outline">not set</Badge>
        )}
        {missing && <Badge variant="destructive">no key for {provider}</Badge>}
        {pendingRemoval && <Badge variant="outline">will be removed on save</Badge>}
        {status?.set &&
          (pendingRemoval ? (
            <Button type="button" size="xs" variant="ghost" onClick={onUndo}>
              Undo
            </Button>
          ) : (
            <Button type="button" size="xs" variant="ghost" onClick={onRemove}>
              Remove
            </Button>
          ))}
      </div>
      <Input
        id={`key-${provider}`}
        type="password"
        autoComplete="off"
        placeholder={status?.set ? "Enter a new key to replace it" : "API key"}
        {...inputProps}
      />
    </div>
  );
}

/**
 * Screen 5: the config form, one component for the wizard's Configure step and
 * (step 13) project settings and global settings. Models per task (a default plus
 * analyze / rationale / chat overrides), provider keys (write-only: a set key
 * shows only its last four characters), endpoints, the GitHub crawl and token,
 * and the git hooks.
 *
 * A save sends the WHOLE layer (the server replaces it), rebuilt from the stored
 * one with only this form's keys overwritten, so keys the form does not show
 * survive. A key the resolved model needs but lacks shows "no key for <provider>".
 */
export function ConfigForm({
  scope,
  submitLabel = "Save",
  onSaved,
  secondaryActions,
}: {
  scope: ConfigScope;
  submitLabel?: string;
  /** Called after a successful save (with the response), or immediately when nothing changed. */
  onSaved?: (saved?: StoredConfig & { cleared_project_keys?: { slug: string; provider: string }[] }) => void;
  secondaryActions?: ReactNode;
}) {
  const queryClient = useQueryClient();
  const slug = scope.kind === "project" ? scope.slug : null;
  const isProject = slug !== null;

  const stored = useQuery({
    queryKey: slug ? projectKey(slug, "config") : portalKey("defaults"),
    queryFn: async (): Promise<StoredConfig> =>
      slug ? projectApi(slug).config() : portalApi.defaults(),
  });
  const globals = useQuery({
    queryKey: portalKey("defaults"),
    queryFn: portalApi.defaults,
    enabled: isProject,
  });
  const project = useQuery({
    queryKey: projectKey(slug ?? "", "project"),
    queryFn: () => portalApi.project(slug!),
    enabled: isProject,
  });

  const defaultValues = useMemo(() => layerToValues(stored.data?.config ?? {}), [stored.data]);
  const form = useForm<ConfigFormValues>({
    resolver: zodResolver(configFormSchema),
    defaultValues,
  });
  const { register, control, handleSubmit, reset, formState } = form;
  const [removed, setRemoved] = useState<string[]>([]);
  const [serverError, setServerError] = useState<ReactNode>(null);

  // Re-seed when the stored layer changes (first load, after a save). Typing does
  // not change `stored.data`, so this never clobbers an edit in progress.
  useEffect(() => {
    if (stored.data) {
      reset(layerToValues(stored.data.config));
      setRemoved([]);
    }
  }, [stored.data, reset]);

  const save = useMutation({
    mutationFn: async (body: ConfigPut) =>
      slug ? projectApi(slug).putConfig(body) : portalApi.putDefaults(body),
    onSuccess: async (saved) => {
      setServerError(null);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: slug ? projectKey(slug, "config") : portalKey("defaults") }),
        queryClient.invalidateQueries({ queryKey: portalKey("defaults") }),
        slug && queryClient.invalidateQueries({ queryKey: projectKey(slug, "project") }),
        // A global save changes what every project inherits (keys, missing-key badges).
        !slug &&
          queryClient.invalidateQueries({
            predicate: (q) =>
              q.queryKey[0] !== "@portal" && (q.queryKey[1] === "project" || q.queryKey[1] === "config"),
          }),
        queryClient.invalidateQueries({ queryKey: portalKey("projects") }),
      ]);
      toast.success("Settings saved");
      onSaved?.(saved);
    },
    onError: (err) => {
      if (err instanceof ApiError) {
        const keys = Array.isArray(err.extra.keys) ? (err.extra.keys as string[]) : [];
        setServerError(
          <>
            {err.message}
            {keys.length > 0 && <span className="font-mono"> ({keys.join(", ")})</span>}
          </>,
        );
      } else {
        setServerError(err instanceof Error ? err.message : "Saving failed.");
      }
    },
  });

  if (stored.isLoading) return <p className="text-sm text-muted-foreground">Loading settings…</p>;
  if (stored.isError || !stored.data) {
    return (
      <Alert variant="destructive">
        <AlertTitle>Could not load the settings</AlertTitle>
        <AlertDescription>{stored.error?.message}</AlertDescription>
      </Alert>
    );
  }

  const secrets = stored.data.secrets;
  const errors = formState.errors;
  const showHooks = isProject && project.data?.source === "local";
  const missingKey = project.data?.missing_key ?? null;
  const noProviderKey = !isProject && stored.data.no_provider_key === true;
  const importReport = isProject ? (stored.data.import ?? null) : null;

  const onSubmit = handleSubmit((values) => {
    const base = stored.data!.config;
    const layer = valuesToLayer(base, values, { scan: isProject });
    const patch = secretsPatch(values, removed, { github: isProject });
    const body: ConfigPut = {};
    if (JSON.stringify(layer) !== JSON.stringify(base)) body.config = layer;
    if (patch) body.secrets = patch;
    if (!body.config && !body.secrets) {
      onSaved?.();
      return;
    }
    save.mutate(body);
  });

  return (
    <form onSubmit={onSubmit} noValidate className="flex flex-col gap-5" data-testid="config-form">
      {noProviderKey && (
        <Alert data-testid="no-provider-key">
          <AlertTitle>No provider key set</AlertTitle>
          <AlertDescription>
            Descriptions, rationale cards and chat need an LLM API key. Keys in your shell
            environment do not reach the portal - enter one below.
          </AlertDescription>
        </Alert>
      )}
      {importReport &&
        (importReport.secrets_moved.length > 0 || importReport.dropped.length > 0) && (
          <Alert>
            <AlertTitle>Imported from whygraph.toml</AlertTitle>
            <AlertDescription>
              {importReport.secrets_moved.length > 0 && (
                <p>
                  Moved into the encrypted store:{" "}
                  <span className="font-mono">{importReport.secrets_moved.join(", ")}</span>. You can
                  delete those lines from the file.
                </p>
              )}
              {importReport.dropped.length > 0 && (
                <ul className="mt-1 list-disc pl-4">
                  {importReport.dropped.map((d) => (
                    <li key={d.key}>
                      <span className="font-mono">{d.key}</span> - {d.hint}
                    </li>
                  ))}
                </ul>
              )}
            </AlertDescription>
          </Alert>
        )}

      <Section
        title="Models"
        description="One default for every task, with optional overrides. Blank means inherit."
      >
        <ModelRow
          name="defaultModel"
          label="Default model"
          hint="Used by every task that has no override. With none set, Anthropic's default applies."
          providers={PROVIDERS}
          emptyLabel="Provider default"
          register={register}
          error={errors.defaultModel?.model?.message ?? errors.defaultModel?.root?.message}
        />
        {TASKS.map((t) => (
          <ModelRow
            key={t.id}
            name={t.id}
            label={t.label}
            hint={t.hint}
            providers={t.id === "chat" ? CHAT_PROVIDER_TAGS : PROVIDERS}
            emptyLabel="Inherit default"
            register={register}
            error={errors[t.id]?.model?.message ?? errors[t.id]?.provider?.message ?? errors[t.id]?.root?.message}
          />
        ))}
      </Section>

      <Section
        title="Provider keys"
        description={
          isProject
            ? "A key set here applies to this project only; without one, the global key is used."
            : "Used by every project unless it sets its own."
        }
      >
        <div className="flex flex-col gap-4">
          {KEYED_PROVIDERS.map((provider) => (
            <KeyRow
              key={provider}
              provider={provider}
              status={secrets.llm[provider]}
              inherited={isProject ? globals.data?.secrets.llm[provider] : undefined}
              missing={missingKey === provider}
              pendingRemoval={removed.includes(provider)}
              onRemove={() => setRemoved((r) => [...r, provider])}
              onUndo={() => setRemoved((r) => r.filter((x) => x !== provider))}
              inputProps={register(`keys.${provider}` as FieldPath<ConfigFormValues>)}
            />
          ))}
        </div>
        <p className="text-xs text-muted-foreground">
          <span className="font-mono">claude-cli</span> and <span className="font-mono">ollama</span>{" "}
          need no API key. Keys are stored encrypted and never shown again.
        </p>
      </Section>

      <Section
        title="Endpoints"
        description={
          <>
            Inside the portal's container, <span className="font-mono">localhost</span> is the
            container itself. Reach a service on your machine as{" "}
            <span className="font-mono">host.docker.internal</span>. Changing an endpoint clears
            that provider's key for this scope.
          </>
        }
      >
        <Field label="OpenAI-compatible base URL" error={errors.openaiBaseUrl?.message}>
          {(p) => (
            <Input
              {...p}
              placeholder="https://api.openai.com/v1"
              className="font-mono"
              {...register("openaiBaseUrl")}
            />
          )}
        </Field>
        <Field label="Ollama host" error={errors.ollamaHost?.message}>
          {(p) => (
            <Input
              {...p}
              placeholder="http://host.docker.internal:11434"
              className="font-mono"
              {...register("ollamaHost")}
            />
          )}
        </Field>
      </Section>

      {isProject && (
        <Section
          title="GitHub"
          description="Pull requests and issues come from the GitHub API and need a token; commits do not."
        >
          <Controller
            control={control}
            name="forge"
            render={({ field }) => (
              <div className="flex items-center justify-between gap-3">
                <Label htmlFor="forge-switch">Fetch pull requests and issues from GitHub</Label>
                <Switch id="forge-switch" checked={field.value} onCheckedChange={field.onChange} />
              </div>
            )}
          />
          <div className="flex flex-col gap-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <Label htmlFor="github-token">GitHub token</Label>
              {secrets.github_token.set ? (
                <Badge variant="secondary">set {secrets.github_token.hint}</Badge>
              ) : (
                <Badge variant="outline">not set</Badge>
              )}
              {removed.includes("github") && <Badge variant="outline">will be removed on save</Badge>}
              {secrets.github_token.set &&
                (removed.includes("github") ? (
                  <Button
                    type="button"
                    size="xs"
                    variant="ghost"
                    onClick={() => setRemoved((r) => r.filter((x) => x !== "github"))}
                  >
                    Undo
                  </Button>
                ) : (
                  <Button
                    type="button"
                    size="xs"
                    variant="ghost"
                    onClick={() => setRemoved((r) => [...r, "github"])}
                  >
                    Remove
                  </Button>
                ))}
            </div>
            <Input
              id="github-token"
              type="password"
              autoComplete="off"
              placeholder={secrets.github_token.set ? "Enter a new token to replace it" : "GitHub token"}
              {...register("githubToken")}
            />
          </div>
        </Section>
      )}

      {showHooks && (
        <Section
          title="Git hooks"
          description="After each of these git events the portal rescans the repository (git history and code structure only - no LLM calls). Initialize installs the ticked ones."
        >
          <div className="grid gap-2 sm:grid-cols-2">
            {HOOK_NAMES.map((hook) => (
              <Controller
                key={hook}
                control={control}
                name={`hooks.${hook}`}
                render={({ field }) => (
                  <label className="flex cursor-pointer items-center gap-2 text-sm">
                    <Checkbox checked={field.value} onCheckedChange={field.onChange} />
                    <span className="font-mono text-[13px]">{hook}</span>
                  </label>
                )}
              />
            ))}
          </div>
        </Section>
      )}
      {isProject && project.data?.source === "github" && (
        <p className="text-xs text-muted-foreground">
          Git hooks are not installed in a GitHub clone; the portal syncs it on a schedule instead.
        </p>
      )}

      {serverError && (
        <Alert variant="destructive" data-testid="save-error">
          <AlertTitle>Could not save</AlertTitle>
          <AlertDescription>{serverError}</AlertDescription>
        </Alert>
      )}

      <div className="flex items-center justify-end gap-2">
        {secondaryActions}
        <Button type="submit" disabled={save.isPending}>
          {save.isPending ? "Saving…" : submitLabel}
        </Button>
      </div>
    </form>
  );
}
