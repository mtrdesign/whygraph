import { useCallback, useEffect, useRef, useState } from "react";
import { Controller, useFieldArray, useForm, useWatch, type Control, type FieldPath, type UseFormReturn } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { PlusIcon, TriangleAlertIcon, Trash2Icon } from "lucide-react";
import {
  portalApi,
  portalKey,
  projectApi,
  projectKey,
  type ConfigDict,
  type ConfigPut,
  type ModelListResponse,
  type ProjectConfigView,
  type SecretsPatch,
} from "../../api";
import {
  CHAT_PROVIDER_TAGS,
  DEFAULT_AGENT_DESCRIPTIONS,
  DEFAULT_AGENT_GENERATIONS,
  HOOK_NAMES,
  KEYED_PROVIDERS,
  PROVIDERS,
  TASKS,
  EFFORT_LEVELS,
  MODE_IDS,
  configFormSchema,
  layerToValues,
  valuesToLayer,
  type ConfigFormValues,
  type TaskName,
} from "../../lib/configForm";
import { isProduction, usePortalState, useReadOnly } from "../../lib/identity";
import { errorMessage } from "../../lib/apiErrors";
import { effortLabel, modeLabel, providerLabel } from "../../lib/labels";
import {
  endpointOverride,
  inheritedChatModeText,
  inheritedEffortText,
  inheritedEndpointText,
  inheritedFrom,
  inheritedModelText,
  inheritedTokenText,
  invalidateKeyDependents,
  keySetText,
  projectKeyStatus,
  revertKeyLabel,
  revertTakesOver,
  tasksWithoutKey,
} from "../../lib/settings";
import { KeyCard } from "../settings/KeyCard";
import { SectionForm } from "../settings/SectionForm";
import { SettingsSection, type SettingsNavItem } from "../settings/SettingsLayout";
import { ErrorState } from "../state/ErrorState";
import { FormSkeleton } from "../state/Skeletons";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Button } from "../ui/button";
import { Checkbox } from "../ui/checkbox";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select";
import { Switch } from "../ui/switch";
import { Textarea } from "../ui/textarea";
import { Field } from "./Field";
import { ModelIdInput } from "./ModelIdInput";

// The project config and the defaults differ in `import` / the key fields
// (project) and `no_provider_key` (defaults).
type StoredConfig = Omit<ProjectConfigView, "import"> & {
  import?: ProjectConfigView["import"];
  no_provider_key?: boolean;
};

/**
 * Where the form reads and writes. `project` edits one project's layer (models,
 * keys, endpoints, GitHub, hooks); `global` edits the defaults every project
 * inherits (models, keys, endpoints, the org limits - rule 6 of §4.2.1).
 */
export type ConfigScope = { kind: "project"; slug: string } | { kind: "global" };

type SectionId = "models" | "github" | "hooks" | "scanning" | "limits" | "areas";

/** Each section's form fields; a section's Save writes these and nothing else. */
const SECTION_FIELDS: Record<SectionId, (keyof ConfigFormValues)[]> = {
  models: ["defaultModel", "analyze", "rationale", "chat", "openaiBaseUrl", "ollamaHost"],
  github: ["forge"],
  hooks: ["hooks"],
  scanning: ["serveCodegraph"],
  limits: ["agentGenerations", "agentDescriptions"],
  areas: ["areas"],
};

const pick = (v: ConfigFormValues, fields: (keyof ConfigFormValues)[]) =>
  Object.fromEntries(fields.map((f) => [f, v[f]])) as Partial<ConfigFormValues>;
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/**
 * The sections `ConfigForm` renders for a scope, in order, for the page's
 * section list: Models and keys and GitHub everywhere a project is; Git hooks
 * for a local folder (local mode); on the defaults, the GitHub token (local
 * mode) and the org Agent limits (production, owners).
 */
export function configSections(
  scope: ConfigScope["kind"],
  { production, source, configurer }: { production: boolean; source?: string; configurer?: boolean },
): SettingsNavItem[] {
  const models = { id: "models", label: "Models and keys" };
  if (scope === "project") {
    return [
      models,
      { id: "github", label: "GitHub" },
      ...(!production && source === "local" ? [{ id: "hooks", label: "Git hooks" }] : []),
      ...(!production && source === "local" ? [{ id: "scanning", label: "Scanning" }] : []),
      { id: "areas", label: "Areas" },
    ];
  }
  return [
    models,
    ...(!production ? [{ id: "github", label: "GitHub token" }] : []),
    ...(production && configurer ? [{ id: "limits", label: "Agent limits" }] : []),
  ];
}

const FALLBACK_REASONS: Record<string, string> = {
  rejected: "the key cannot list models",
  key_missing: "no key set",
  throttled: "listing is rate limited",
  rate_limited: "listing is rate limited",
};
const FALLBACK_REASON_DEFAULT = "the provider could not be reached";

// A Select item cannot carry "" (the form's "inherit"), so it travels as this.
const INHERIT = "inherit";

/** A styled `Select` bound to a form string where "" means inherit, like the model field beside it. */
function FormSelect({
  value,
  onChange,
  items,
  disabled,
  className,
  ...trigger
}: {
  value: string;
  onChange: (value: string) => void;
  /** The options; the one with value "" is the inherit choice. */
  items: { value: string; label: string }[];
  disabled?: boolean;
  className: string;
  id?: string;
  "aria-label": string;
  title?: string;
}) {
  const selectItems = items.map((item) => ({ ...item, value: item.value || INHERIT }));
  return (
    <Select
      items={selectItems}
      value={value || INHERIT}
      disabled={disabled}
      onValueChange={(next) => {
        if (next) onChange(next === INHERIT ? "" : next);
      }}
    >
      <SelectTrigger {...trigger} className={className}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false}>
        {selectItems.map((item) => (
          <SelectItem key={item.value} value={item.value}>
            {item.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function ModelRow({
  name,
  label,
  hint,
  providers,
  emptyLabel,
  placeholder,
  control,
  readOnly,
  scope,
  canList,
  error,
  overridden,
  onReset,
  effortEmptyLabel,
  modeEmptyLabel,
}: {
  name: "defaultModel" | TaskName;
  label: string;
  hint: string;
  providers: string[];
  emptyLabel: string;
  placeholder: string;
  /** The parent's `form.control` (there is no FormProvider). */
  control: Control<ConfigFormValues>;
  /** Base UI's Select is not a native select: `<fieldset disabled>` alone leaves it looking enabled (SET-4). */
  readOnly: boolean;
  scope: ConfigScope;
  /** The form's `canTest`: only people the listing routes admit ask for suggestions. */
  canList: boolean;
  error?: string;
  /** A project's own value over an inherited one: "Overridden here" with Reset. */
  overridden?: boolean;
  onReset?: () => void;
  /** Set on the Chat row only: shows the Effort select, whose empty option reads this. */
  effortEmptyLabel?: string;
  /** Set on the Chat row at project scope only: shows the Mode select, whose empty option reads this. */
  modeEmptyLabel?: string;
}) {
  const provider = (useWatch({ control, name: `${name}.provider` as FieldPath<ConfigFormValues> }) ?? "") as string;
  const listing = useQuery<ModelListResponse>({
    queryKey: scope.kind === "project" ? projectKey(scope.slug, "models", provider) : portalKey("models", provider),
    queryFn: () => (scope.kind === "project" ? projectApi(scope.slug).models(provider) : portalApi.defaultsModels(provider)),
    enabled: canList && KEYED_PROVIDERS.includes(provider),
    // Model catalogues barely move within a session, and OpenRouter's is a
    // ~370-entry payload - no need to refetch on every mount.
    staleTime: 10 * 60 * 1000,
    retry: false,
  });
  const suggestions = canList && KEYED_PROVIDERS.includes(provider) ? listing.data : undefined;
  return (
    <Field label={label} hint={hint} error={error}>
      {(p) => (
        <div className="flex flex-col gap-1.5">
          <div className="flex flex-wrap gap-2">
            <Controller
              control={control}
              name={`${name}.provider` as FieldPath<ConfigFormValues>}
              render={({ field }) => (
                <FormSelect
                  aria-label={`${label} provider`}
                  className="w-full sm:w-44"
                  value={(field.value ?? "") as string}
                  onChange={field.onChange}
                  disabled={readOnly}
                  items={[
                    { value: "", label: emptyLabel },
                    ...providers.map((prov) => ({ value: prov, label: providerLabel(prov) })),
                  ]}
                />
              )}
            />
            <Controller
              control={control}
              name={`${name}.model` as FieldPath<ConfigFormValues>}
              render={({ field }) => (
                <ModelIdInput
                  fieldProps={p}
                  value={(field.value ?? "") as string}
                  onChange={field.onChange}
                  onBlur={field.onBlur}
                  inputRef={field.ref}
                  models={suggestions?.models}
                  label={label}
                  placeholder={placeholder}
                />
              )}
            />
          </div>
          {suggestions?.source === "fallback" && (
            <span className="text-xs text-muted-foreground" data-testid="model-suggest-fallback">
              Suggestions are a short built-in list ({FALLBACK_REASONS[suggestions.result] ?? FALLBACK_REASON_DEFAULT}). Any
              model id works.
            </span>
          )}
          {effortEmptyLabel !== undefined && (
            <div className="flex flex-wrap items-center gap-2">
              <label htmlFor={`${p.id}-effort`} className="text-xs text-muted-foreground">
                Effort
              </label>
              <Controller
                control={control}
                name="chat.effort"
                render={({ field }) => (
                  <FormSelect
                    id={`${p.id}-effort`}
                    aria-label="Effort"
                    title="Default effort for chats that have no level of their own."
                    className="w-full sm:w-64"
                    value={(field.value ?? "") as string}
                    onChange={field.onChange}
                    disabled={readOnly}
                    items={[
                      { value: "", label: effortEmptyLabel },
                      ...EFFORT_LEVELS.map((level) => ({ value: level as string, label: effortLabel(level) })),
                    ]}
                  />
                )}
              />
            </div>
          )}
          {modeEmptyLabel !== undefined && (
            <div className="flex flex-wrap items-center gap-2">
              <label htmlFor={`${p.id}-mode`} className="text-xs text-muted-foreground">
                Mode
              </label>
              <Controller
                control={control}
                name="chat.mode"
                render={({ field }) => {
                  const stored = (field.value ?? "") as string;
                  return (
                    <FormSelect
                      id={`${p.id}-mode`}
                      aria-label="Mode"
                      title="Default mode for new chats in this project."
                      className="w-full sm:w-64"
                      value={stored}
                      onChange={field.onChange}
                      disabled={readOnly}
                      items={[
                        { value: "", label: modeEmptyLabel },
                        // A stored id this build does not know stays visible, and saving keeps it.
                        ...(stored !== "" && !(MODE_IDS as readonly string[]).includes(stored)
                          ? [{ value: stored, label: `${stored} (unknown)` }]
                          : []),
                        ...MODE_IDS.map((id) => ({ value: id as string, label: modeLabel(id) })),
                      ]}
                    />
                  );
                }}
              />
            </div>
          )}
          {overridden && onReset && <OverriddenHere onReset={onReset} label={label} />}
        </div>
      )}
    </Field>
  );
}

function OverriddenHere({ onReset, label }: { onReset: () => void; label: string }) {
  return (
    <span className="row-wrap gap-2 text-xs text-muted-foreground" data-testid="overridden-here">
      Overridden here
      <button
        type="button"
        onClick={onReset}
        aria-label={`Reset ${label}`}
        className="text-primary-text underline-offset-4 hover:underline disabled:pointer-events-none disabled:opacity-50"
      >
        Reset
      </button>
    </span>
  );
}

/**
 * The config sections of project settings and of the org / global settings
 * (SET-2, SET-3, SET-6, R3): Models and keys, GitHub, Git hooks and Agent limits.
 * Each section saves its own fields with its own Save / Discard; a key card's
 * Replace / Remove goes out at once, with only that secret.
 *
 * A save sends the WHOLE layer (the server replaces it), rebuilt from the stored
 * one with only that section's fields overwritten, so other sections' unsaved
 * edits and keys the form does not show survive. An empty project field shows
 * the value it inherits ("Inherited: ... from the organization", locally "from
 * portal defaults"); a set one says "Overridden here" with Reset.
 *
 * In production a project is a server copy read through the GitHub App: no git
 * hooks and no GitHub token (no PATs in production); the forge toggle stays.
 */
export function ConfigForm({
  scope,
  onSaved,
  readOnly: readOnlyProp = false,
}: {
  scope: ConfigScope;
  /** Called after a successful section save, with the response. */
  onSaved?: (saved: StoredConfig & { cleared_project_keys?: { slug: string; provider: string }[] }) => void;
  /** Show the settings without a way to save them (besides a `reader`, who never can). */
  readOnly?: boolean;
}) {
  const queryClient = useQueryClient();
  const reader = useReadOnly();
  const state = usePortalState().data;
  const production = isProduction(state);
  const mode = state?.mode;
  const slug = scope.kind === "project" ? scope.slug : null;
  const isProject = slug !== null;
  const configKey = slug ? projectKey(slug, "config") : portalKey("defaults");

  const stored = useQuery({
    queryKey: configKey,
    queryFn: async (): Promise<StoredConfig> => (slug ? projectApi(slug).config() : portalApi.defaults()),
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

  const form = useForm<ConfigFormValues>({
    resolver: zodResolver(configFormSchema),
    defaultValues: layerToValues({}),
  });
  const { register, control, reset, getValues, setValue, trigger, clearErrors, formState } = form;
  const values = useWatch({ control }) as ConfigFormValues;
  // The stored layer as form values: what each section's fields are compared with.
  const [base, setBase] = useState<ConfigFormValues | null>(null);
  const baseRef = useRef<ConfigFormValues | null>(null);

  // Re-seed from a stored layer (first load, a refetch, a save's answer). A
  // section with unsaved edits keeps them; the one just saved takes the stored values.
  const reseed = useCallback(
    (config: ConfigDict, saved: SectionId | null) => {
      const fresh = layerToValues(config);
      const prev = baseRef.current;
      const current = getValues();
      const next = { ...fresh };
      if (prev) {
        for (const [section, fields] of Object.entries(SECTION_FIELDS) as [SectionId, (keyof ConfigFormValues)[]][]) {
          if (section === saved) continue;
          if (!same(pick(current, fields), pick(prev, fields))) Object.assign(next, pick(current, fields));
        }
      }
      reset(next);
      baseRef.current = fresh;
      setBase(fresh);
    },
    [getValues, reset],
  );
  useEffect(() => {
    if (stored.data) reseed(stored.data.config, null);
  }, [stored.data, reseed]);

  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: configKey }),
      queryClient.invalidateQueries({ queryKey: portalKey("defaults") }),
      slug && queryClient.invalidateQueries({ queryKey: projectKey(slug, "project") }),
      // A defaults save changes what every project inherits (keys, missing-key notes).
      !slug &&
        queryClient.invalidateQueries({
          predicate: (q) =>
            q.queryKey[0] !== "@portal" && (q.queryKey[1] === "project" || q.queryKey[1] === "config"),
        }),
      queryClient.invalidateQueries({ queryKey: portalKey("projects") }),
      invalidateKeyDependents(queryClient),
    ]);
  };
  const put = useMutation({
    mutationFn: async (body: ConfigPut) => (slug ? projectApi(slug).putConfig(body) : portalApi.putDefaults(body)),
    onSuccess: invalidate,
  });
  const putSecret = (secrets: SecretsPatch) => put.mutateAsync({ secrets });

  const sections = configSections(scope.kind, {
    production,
    source: project.data?.source,
    configurer: stored.data ? !stored.data.read_only : !readOnlyProp,
  });

  if (stored.isLoading || (stored.data && !base)) {
    return (
      <>
        {sections.map((s) => (
          <SettingsSection key={s.id} id={s.id} title={s.label}>
            <FormSkeleton fields={s.id === "models" ? 4 : 1} label={`Loading ${s.label}`} />
          </SettingsSection>
        ))}
      </>
    );
  }
  if (stored.isError || !stored.data || !base) {
    return (
      <SettingsSection id="models" title="Models and keys">
        <ErrorState
          error={stored.error}
          title="Couldn't load the settings"
          onRetry={() => void stored.refetch()}
        />
      </SettingsSection>
    );
  }

  const view = stored.data;
  const readOnly = readOnlyProp || reader || view.read_only === true;
  const canTest = !readOnly && view.can_test_keys === true;
  const layer = view.config;
  const orgLayer = isProject ? (globals.data?.config ?? null) : null;
  const orgSecrets = isProject ? globals.data?.secrets : undefined;
  const secrets = view.secrets;
  const errors = formState.errors;
  const showHooks = isProject && !production && project.data?.source === "local";
  const showLimits = !isProject && production && !readOnly;
  const missingKey = project.data?.missing_key ?? null;
  const importReport = isProject ? (view.import ?? null) : null;
  const lastUsed = view.key_last_used;

  const dirty = (section: SectionId) => !same(pick(values, SECTION_FIELDS[section]), pick(base, SECTION_FIELDS[section]));
  const discard = (section: SectionId) => {
    const fields = SECTION_FIELDS[section];
    for (const f of fields) setValue(f, base[f] as never);
    clearErrors(fields as FieldPath<ConfigFormValues>[]);
  };
  const saveSection = async (section: SectionId) => {
    const fields = SECTION_FIELDS[section];
    if (!(await trigger(fields as FieldPath<ConfigFormValues>[]))) return false;
    const merged = { ...base, ...pick(getValues(), fields) } as ConfigFormValues;
    const next = valuesToLayer(layer, merged, { scan: isProject, limits: section === "limits", areas: section === "areas" });
    const saved = await put.mutateAsync({ config: next });
    // The answer is the stored layer: this section is clean even when the
    // refetch brings back an identical payload (no new `stored.data`).
    reseed((saved as StoredConfig).config ?? next, section);
    onSaved?.(saved as StoredConfig);
    return true;
  };
  const resetFields = (...pairs: [FieldPath<ConfigFormValues>, unknown][]) => {
    for (const [f, v] of pairs) setValue(f, v as never);
  };

  const taskWarnings = !isProject && !view.no_provider_key ? tasksWithoutKey(layer, secrets.llm) : [];
  const orgEndpoint = (provider: "openai" | "ollama", key: "base_url" | "host") => {
    const llm = (orgLayer?.llm ?? {}) as Record<string, Record<string, unknown> | undefined>;
    const v = llm[provider]?.[key];
    return typeof v === "string" ? v : "";
  };

  return (
    <>
      <SettingsSection
        id="models"
        title="Models and keys"
        description={
          isProject
            ? `A project key or model wins over the one it inherits ${inheritedFrom(mode)}.`
            : "Used by every project unless it sets its own."
        }
      >
        {view.no_provider_key === true && (
          <Alert variant="warning" data-testid="no-provider-key">
            <TriangleAlertIcon />
            <AlertTitle>No provider key</AlertTitle>
            <AlertDescription>
              Descriptions, rationale cards and chat need an LLM API key.
              {production
                ? " Add one below."
                : " Keys in your shell environment do not reach the portal - add one below."}
            </AlertDescription>
          </Alert>
        )}
        {taskWarnings.length > 0 && (
          <Alert variant="warning" data-testid="task-key-warnings">
            <TriangleAlertIcon />
            <AlertTitle>A task has no key</AlertTitle>
            <AlertDescription>
              <ul className="list-disc pl-4">
                {taskWarnings.map((w) => (
                  <li key={w}>{w}.</li>
                ))}
              </ul>
            </AlertDescription>
          </Alert>
        )}
        {importReport && (importReport.secrets_moved.length > 0 || importReport.dropped.length > 0) && (
          <Alert>
            <AlertTitle>Imported from whygraph.toml</AlertTitle>
            <AlertDescription>
              {importReport.secrets_moved.length > 0 && (
                <p>
                  Moved into the encrypted store:{" "}
                  <span className="font-mono">{importReport.secrets_moved.join(", ")}</span>. You can delete
                  those lines from the file.
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

        <div className="flex flex-col gap-2">
          <h3 className="text-[13px] font-medium">Provider keys</h3>
          {KEYED_PROVIDERS.map((provider) => {
            const label = providerLabel(provider);
            const own = secrets?.llm[provider];
            if (!isProject) {
              return (
                <KeyCard
                  key={provider}
                  label={label}
                  testId={`key-${provider}`}
                  status={own?.set ? keySetText(own) : "No key"}
                  isSet={!!own?.set}
                  lastUsed={lastUsed && own?.set ? (lastUsed.llm[provider] ?? null) : undefined}
                  readOnly={readOnly}
                  onTest={canTest && own?.set ? () => portalApi.defaultsKeyTest(provider) : undefined}
                  onSave={(v) => putSecret({ llm: { [provider]: v } })}
                  onRemove={() => putSecret({ llm: { [provider]: null } })}
                  removeConfirm={{
                    title: `Remove the ${label} key?`,
                    description: `Projects without their own ${label} key stop using one.`,
                  }}
                />
              );
            }
            const endpoint = endpointOverride(layer, provider);
            const scope =
              view.effective_keys?.[provider] ??
              (own?.set ? "project" : orgSecrets?.llm[provider]?.set && !endpoint ? "org" : "none");
            const inheritedSet =
              view.inherited?.[provider]?.set ?? (!!orgSecrets?.llm[provider]?.set && !endpoint);
            const orgHint = orgSecrets?.llm[provider]?.hint ?? null;
            const revert = inheritedSet ? revertKeyLabel(mode) : "Remove";
            return (
              <KeyCard
                key={provider}
                label={label}
                testId={`key-${provider}`}
                status={projectKeyStatus({ provider, own, scope, inheritedHint: orgHint, endpoint, mode })}
                isSet={!!own?.set}
                lastUsed={lastUsed && scope !== "none" ? (lastUsed.llm[provider] ?? null) : undefined}
                warning={missingKey === provider ? `A task uses ${label}, which has no key.` : undefined}
                readOnly={readOnly}
                onTest={canTest && scope !== "none" ? () => projectApi(slug).keyTest(provider) : undefined}
                onSave={(v) => putSecret({ llm: { [provider]: v } })}
                onRemove={() => putSecret({ llm: { [provider]: null } })}
                removeLabel={revert}
                removeConfirm={{
                  title: inheritedSet ? `${revert}?` : `Remove the ${label} key?`,
                  description: revertTakesOver(mode, inheritedSet, orgHint),
                }}
              />
            );
          })}
          <p className="text-xs text-muted-foreground">
            Ollama needs no API key. Keys are stored encrypted and never shown again.
          </p>
        </div>

        <SectionForm
          name="Models and keys"
          testId="config-form"
          dirty={dirty("models")}
          readOnly={readOnly}
          onSave={() => saveSection("models")}
          onDiscard={() => discard("models")}
        >
          <div className="flex flex-col gap-4">
            <div>
              <h3 className="text-[13px] font-medium">Models</h3>
              <p className="text-xs text-muted-foreground">
                One default for every task, with optional overrides. Blank means inherit.
              </p>
            </div>
            <ModelRow
              name="defaultModel"
              label="Default model"
              hint="Used by every task that has no override."
              providers={PROVIDERS}
              emptyLabel={isProject ? "Inherited" : "Provider default"}
              placeholder={inheritedModelText({ task: null, projectLayer: isProject ? layer : null, orgLayer, mode })}
              control={control}
              readOnly={readOnly}
              scope={scope}
              canList={canTest}
              error={errors.defaultModel?.model?.message ?? errors.defaultModel?.root?.message}
              overridden={isProject && !!(values.defaultModel?.provider || values.defaultModel?.model)}
              onReset={() => resetFields(["defaultModel", { provider: "", model: "" }])}
            />
            {TASKS.map((t) => (
              <ModelRow
                key={t.id}
                name={t.id}
                label={t.label}
                hint={t.hint}
                providers={t.id === "chat" ? CHAT_PROVIDER_TAGS : PROVIDERS}
                emptyLabel={isProject ? "Inherited" : "Default model's"}
                placeholder={inheritedModelText({ task: t.id, projectLayer: isProject ? layer : null, orgLayer, mode })}
                control={control}
                readOnly={readOnly}
                scope={scope}
                canList={canTest}
                error={errors[t.id]?.model?.message ?? errors[t.id]?.provider?.message ?? errors[t.id]?.root?.message}
                overridden={
                  isProject &&
                  !!(values[t.id]?.provider || values[t.id]?.model || (t.id === "chat" && (values.chat?.effort || values.chat?.mode)))
                }
                onReset={() =>
                  resetFields([t.id, t.id === "chat" ? { provider: "", model: "", effort: "", mode: "" } : { provider: "", model: "" }])
                }
                effortEmptyLabel={
                  t.id === "chat"
                    ? inheritedEffortText({ projectLayer: isProject ? layer : null, orgLayer, mode })
                    : undefined
                }
                modeEmptyLabel={t.id === "chat" && isProject ? inheritedChatModeText() : undefined}
              />
            ))}
          </div>
          <div className="flex flex-col gap-4 border-t border-border pt-4">
            <div>
              <h3 className="text-[13px] font-medium">Endpoints</h3>
              <p className="text-xs text-muted-foreground">
                {!production && (
                  <>
                    Inside the portal's container, <span className="font-mono">localhost</span> is the container
                    itself. Reach a service on your machine as{" "}
                    <span className="font-mono">host.docker.internal</span>.{" "}
                  </>
                )}
                Changing an endpoint clears that provider's key {isProject ? "for this project" : "here"}.
              </p>
            </div>
            <Field label="OpenAI-compatible base URL" error={errors.openaiBaseUrl?.message}>
              {(p) => (
                <div className="flex flex-col gap-1.5">
                  <Input
                    {...p}
                    placeholder={inheritedEndpointText(orgEndpoint("openai", "base_url"), "https://api.openai.com/v1", mode)}
                    className="font-mono"
                    {...register("openaiBaseUrl")}
                  />
                  {isProject && !!values.openaiBaseUrl && (
                    <OverriddenHere label="OpenAI-compatible base URL" onReset={() => resetFields(["openaiBaseUrl", ""])} />
                  )}
                </div>
              )}
            </Field>
            <Field label="Ollama host" error={errors.ollamaHost?.message}>
              {(p) => (
                <div className="flex flex-col gap-1.5">
                  <Input
                    {...p}
                    placeholder={inheritedEndpointText(
                      orgEndpoint("ollama", "host"),
                      production ? "http://ollama.internal:11434" : "http://host.docker.internal:11434",
                      mode,
                    )}
                    className="font-mono"
                    {...register("ollamaHost")}
                  />
                  {isProject && !!values.ollamaHost && (
                    <OverriddenHere label="Ollama host" onReset={() => resetFields(["ollamaHost", ""])} />
                  )}
                </div>
              )}
            </Field>
          </div>
        </SectionForm>
      </SettingsSection>

      {isProject && (
        <SettingsSection
          id="github"
          title="GitHub"
          description={
            production
              ? "Pull requests and issues come from the GitHub API, read through the WhyGraph GitHub App."
              : "Pull requests and issues come from the GitHub API and need a token; commits do not."
          }
        >
          <SectionForm
            name="GitHub"
            dirty={dirty("github")}
            readOnly={readOnly}
            onSave={() => saveSection("github")}
            onDiscard={() => discard("github")}
          >
            <Controller
              control={control}
              name="forge"
              render={({ field }) => (
                <div className="row-wrap justify-between gap-3">
                  <Label htmlFor="forge-switch" className="cursor-pointer">Fetch pull requests and issues from GitHub</Label>
                  {/* Base UI's Switch is a span: `<fieldset disabled>` does not reach it (SET-4). */}
                  <Switch
                    id="forge-switch"
                    checked={field.value}
                    onCheckedChange={field.onChange}
                    disabled={readOnly}
                  />
                </div>
              )}
            />
          </SectionForm>
          {!production && (
            <GitHubTokenCard
              view={view}
              orgHint={orgSecrets?.github_token?.hint ?? null}
              orgSet={!!orgSecrets?.github_token?.set}
              mode={mode}
              readOnly={readOnly}
              onTest={canTest && view.github?.remote ? () => projectApi(slug).githubTokenTest() : undefined}
              putSecret={putSecret}
            />
          )}
        </SettingsSection>
      )}

      {!isProject && !production && (
        <SettingsSection
          id="github"
          title="GitHub token"
          description="Used to fetch pull requests and issues for every project that has no token of its own."
        >
          <KeyCard
            label="GitHub token"
            testId="key-github"
            github
            status={secrets.github_token.set ? keySetText(secrets.github_token) : "No token"}
            isSet={secrets.github_token.set}
            lastUsed={lastUsed && secrets.github_token.set ? lastUsed.github_token : undefined}
            note="Tested per project: a token is checked against each project's own repository, from its settings."
            readOnly={readOnly}
            onSave={(v) => putSecret({ github_token: v })}
            onRemove={() => putSecret({ github_token: null })}
            removeConfirm={{
              title: "Remove the GitHub token?",
              description: "Projects without their own token stop fetching pull requests and issues.",
            }}
          />
        </SettingsSection>
      )}

      {showHooks && (
        <SettingsSection
          id="hooks"
          title="Git hooks"
          description="After each of these git events the portal rescans the repository (git history and code structure only - no LLM calls). Saving installs the ticked ones and removes the rest."
        >
          <SectionForm
            name="Git hooks"
            dirty={dirty("hooks")}
            readOnly={readOnly}
            onSave={() => saveSection("hooks")}
            onDiscard={() => discard("hooks")}
          >
            <div className="grid gap-2 sm:grid-cols-2">
              {HOOK_NAMES.map((hook) => (
                <Controller
                  key={hook}
                  control={control}
                  name={`hooks.${hook}`}
                  render={({ field }) => (
                    <label className="flex cursor-pointer items-center gap-2 text-sm">
                      <Checkbox checked={field.value} onCheckedChange={field.onChange} disabled={readOnly} />
                      <span className="font-mono text-[13px]">{hook}</span>
                    </label>
                  )}
                />
              ))}
            </div>
          </SectionForm>
        </SettingsSection>
      )}

      {showHooks && (
        <SettingsSection
          id="scanning"
          title="Scanning"
          description="Hosted CodeGraph: the portal serves CodeGraph's code tools to this project's agents (codegraph_explore, codegraph_search and the rest), refreshing the code index before it answers."
        >
          <SectionForm
            name="Scanning"
            dirty={dirty("scanning")}
            readOnly={readOnly}
            onSave={() => saveSection("scanning")}
            onDiscard={() => discard("scanning")}
          >
            <Controller
              control={control}
              name="serveCodegraph"
              render={({ field }) => (
                <div className="row-wrap justify-between gap-3">
                  <Label htmlFor="serve-codegraph-switch" className="cursor-pointer">Serve CodeGraph tools to agents</Label>
                  <Switch
                    id="serve-codegraph-switch"
                    checked={field.value}
                    onCheckedChange={field.onChange}
                    disabled={readOnly}
                  />
                </div>
              )}
            />
          </SectionForm>
        </SettingsSection>
      )}

      {isProject && (
        <AreasSection
          slug={slug}
          form={form}
          readOnly={readOnly}
          dirty={dirty("areas")}
          onSave={() => saveSection("areas")}
          onDiscard={() => discard("areas")}
        />
      )}

      {showLimits && (
        <SettingsSection
          id="limits"
          title="Agent limits"
          description="How much language-model work agents connected to a project may trigger, across the whole organization. They apply to every project; a project cannot override them."
        >
          <SectionForm
            name="Agent limits"
            dirty={dirty("limits")}
            readOnly={readOnly}
            onSave={() => saveSection("limits")}
            onDiscard={() => discard("limits")}
          >
            <Field
              label="Rationale cards per hour"
              hint={`Generated when an agent asks for a rationale. Blank = ${DEFAULT_AGENT_GENERATIONS}; 0 turns it off.`}
              error={errors.agentGenerations?.message}
            >
              {(p) => (
                <Input
                  {...p}
                  inputMode="numeric"
                  placeholder={String(DEFAULT_AGENT_GENERATIONS)}
                  className="max-w-40 font-mono"
                  {...register("agentGenerations")}
                />
              )}
            </Field>
            <Field
              label="Commit descriptions per hour"
              hint={`Generated when an agent asks for evidence on undescribed commits. Blank = ${DEFAULT_AGENT_DESCRIPTIONS}; 0 turns it off.`}
              error={errors.agentDescriptions?.message}
            >
              {(p) => (
                <Input
                  {...p}
                  inputMode="numeric"
                  placeholder={String(DEFAULT_AGENT_DESCRIPTIONS)}
                  className="max-w-40 font-mono"
                  {...register("agentDescriptions")}
                />
              )}
            </Field>
          </SectionForm>
        </SettingsSection>
      )}
    </>
  );
}

/**
 * The project's `[areas]`: named parts of the codebase (paths and labels) the chat
 * assistant resolves a question about a feature to. Rows are typed in place;
 * "Suggest from the tree" appends candidates and never saves.
 */
function AreasSection({
  slug,
  form,
  readOnly,
  dirty,
  onSave,
  onDiscard,
}: {
  slug: string;
  form: UseFormReturn<ConfigFormValues>;
  readOnly: boolean;
  dirty: boolean;
  onSave: () => Promise<unknown>;
  onDiscard: () => void;
}) {
  const { control, register, getValues, formState } = form;
  const { fields, append, remove } = useFieldArray({ control, name: "areas" });
  const suggest = useMutation({ mutationFn: () => projectApi(slug).areasSuggest() });
  const rowErrors = formState.errors.areas;
  const tableError = rowErrors && !Array.isArray(rowErrors) ? rowErrors.root?.message ?? rowErrors.message : undefined;
  const addSuggestions = async () => {
    const { suggestions } = await suggest.mutateAsync();
    const have = new Set(getValues("areas").map((r) => r.name.trim()));
    for (const s of suggestions) {
      if (have.has(s.name)) continue;
      append({
        name: s.name,
        description: s.description,
        pathsText: s.paths.join("\n"),
        labelsText: s.labels.join("\n"),
      });
    }
  };
  return (
    <SettingsSection
      id="areas"
      title="Areas"
      description="Name the parts of this codebase (by path and by issue label) so a question about a feature resolves to the right code. Chat shows the areas to the model and can filter history by area."
    >
      <SectionForm
        name="Areas"
        dirty={dirty}
        readOnly={readOnly}
        onSave={onSave}
        onDiscard={onDiscard}
      >
        {fields.length === 0 && <p className="text-sm text-muted-foreground">No areas yet.</p>}
        {fields.map((f, i) => {
          const err = Array.isArray(rowErrors) ? rowErrors[i] : undefined;
          return (
            <div key={f.id} role="group" aria-label={`Area ${i + 1}`} className="flex flex-col gap-3 rounded-lg border p-3">
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Name" hint="Lowercase letters, digits and dashes." error={err?.name?.message}>
                  {(p) => <Input {...p} className="font-mono" {...register(`areas.${i}.name`)} />}
                </Field>
                <Field label="Description" hint="One line; optional." error={err?.description?.message}>
                  {(p) => <Input {...p} {...register(`areas.${i}.description`)} />}
                </Field>
                <Field label="Paths" hint="One per line, relative to the repository; a folder ends in /." error={err?.pathsText?.message}>
                  {(p) => <Textarea {...p} className="font-mono" {...register(`areas.${i}.pathsText`)} />}
                </Field>
                <Field label="Labels" hint="Issue and PR labels, one per line; optional." error={err?.labelsText?.message}>
                  {(p) => <Textarea {...p} className="font-mono" {...register(`areas.${i}.labelsText`)} />}
                </Field>
              </div>
              {!readOnly && (
                <div>
                  <Button type="button" variant="outline" size="sm" onClick={() => remove(i)} aria-label={`Remove area ${i + 1}`}>
                    <Trash2Icon /> Remove
                  </Button>
                </div>
              )}
            </div>
          );
        })}
        {tableError && (
          <p role="alert" className="text-xs text-destructive">
            {tableError}
          </p>
        )}
        {suggest.isError && (
          <p role="alert" className="text-xs text-destructive">
            {errorMessage(suggest.error)}
          </p>
        )}
        {!readOnly && (
          <div className="row-wrap gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => append({ name: "", description: "", pathsText: "", labelsText: "" })}
            >
              <PlusIcon /> Add area
            </Button>
            <Button type="button" variant="outline" size="sm" disabled={suggest.isPending} onClick={() => void addSuggestions()}>
              Suggest from the tree
            </Button>
          </div>
        )}
      </SectionForm>
    </SettingsSection>
  );
}

/** A project's GitHub token card (local mode): its own, the inherited default, or none. */
function GitHubTokenCard({
  view,
  orgHint,
  orgSet,
  mode,
  readOnly,
  onTest,
  putSecret,
}: {
  view: StoredConfig;
  orgHint: string | null;
  orgSet: boolean;
  mode: string | undefined;
  readOnly: boolean;
  onTest?: () => ReturnType<ReturnType<typeof projectApi>["githubTokenTest"]>;
  putSecret: (secrets: SecretsPatch) => Promise<unknown>;
}) {
  const own = view.secrets.github_token;
  const scope = view.github?.token ?? (own.set ? "project" : orgSet ? "org" : "none");
  const status =
    scope === "project"
      ? keySetText(own)
      : scope === "org"
        ? `${inheritedTokenText(mode)}${orgHint ? ` ${orgHint}` : ""}`
        : "No token";
  const remote = view.github?.remote;
  const inherited = orgSet;
  const revert = inherited
    ? mode === "production"
      ? "Revert to the organization's token"
      : "Revert to the portal default token"
    : "Remove";
  return (
    <KeyCard
      label="GitHub token"
      testId="key-github"
      github
      status={status}
      isSet={own.set}
      lastUsed={view.key_last_used && scope !== "none" ? view.key_last_used.github_token : undefined}
      note={
        view.github === undefined
          ? undefined
          : remote
            ? (
                <>
                  Used for <span className="font-mono">{remote}</span>.
                </>
              )
            : "This project's remote is not on GitHub, so pull requests and issues are not fetched."
      }
      readOnly={readOnly}
      onTest={scope !== "none" ? onTest : undefined}
      onSave={(v) => putSecret({ github_token: v })}
      onRemove={() => putSecret({ github_token: null })}
      removeLabel={revert}
      removeConfirm={{
        title: inherited ? `${revert}?` : "Remove the GitHub token?",
        description: inherited
          ? `${mode === "production" ? "The organization's token" : "The portal default token"}${orgHint ? ` (${orgHint})` : ""} will be used.`
          : "No token will be used, so pull requests and issues are not fetched.",
      }}
    />
  );
}

