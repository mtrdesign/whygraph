import { DatabaseIcon, GitBranchIcon, PlugIcon, TriangleAlertIcon } from "lucide-react";
import type { CustomDbPath, Detected } from "../../api";
import { agentInfo } from "../../lib/agents";
import { cn } from "@/lib/utils";
import { Alert, AlertDescription, AlertTitle } from "../ui/alert";
import { Badge } from "../ui/badge";
import { Checkbox } from "../ui/checkbox";

/** Custom DB path warnings (screen 3, shown again on Initialize while the file exists). */
export function CustomDbWarnings({ paths }: { paths: readonly CustomDbPath[] }) {
  const live = paths.filter((p) => p.exists);
  if (live.length === 0) return null;
  return (
    <>
      {live.map((p) => (
        <Alert key={`${p.key}:${p.path}`} data-testid="custom-db-warning">
          <TriangleAlertIcon />
          <AlertTitle>Data at a custom path will not be used</AlertTitle>
          <AlertDescription>
            <p>{p.message}</p>
            <p className="mt-1 font-mono text-xs text-foreground">{p.path}</p>
          </AlertDescription>
        </Alert>
      ))}
    </>
  );
}

function Item({
  icon,
  title,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <li className="flex gap-3 py-2.5">
      <span className="mt-0.5 text-muted-foreground [&_svg]:size-4">{icon}</span>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <span className="text-sm font-medium">{title}</span>
        {children}
      </div>
    </li>
  );
}

const CODEGRAPH_NOTE = "Your own CodeGraph server - the portal serves CodeGraph for this project.";

/**
 * A `codegraph` entry in the user's own agent config that is not the portal's.
 * Left as it is unless "Replace with the portal's CodeGraph" is ticked (the
 * file's path then goes in `replace_codegraph`).
 */
export function ForeignCodegraphEntry({
  file,
  detail,
  tracked,
  replace,
  onReplaceChange,
}: {
  file: string;
  detail?: string;
  tracked?: boolean;
  replace: boolean;
  onReplaceChange: (on: boolean) => void;
}) {
  return (
    <Item icon={<TriangleAlertIcon />} title={CODEGRAPH_NOTE}>
      <p className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground" data-testid="foreign-codegraph">
        <span className="font-mono">{file}</span>
        {detail && <span>{detail}</span>}
        {tracked && <Badge variant="secondary">committed to git</Badge>}
      </p>
      <label className="mt-1 flex cursor-pointer items-center gap-2 text-xs">
        <Checkbox checked={replace} onCheckedChange={(on) => onReplaceChange(on === true)} />
        <span>Replace with the portal's CodeGraph</span>
      </label>
    </Item>
  );
}

const SHAPE_LABEL = { stdio: "1.x stdio entry", http: "HTTP entry", unknown: "unrecognized entry" } as const;

/**
 * The "existing WhyGraph data" panel (screen 3, from the add response's
 * `detected`): a reused database, the 1.x hook blocks, every `whygraph` agent
 * entry (each one migrated to HTTP or removed), and custom DB path warnings.
 * `removed` holds the agents whose entry the user chose to strip; the rest of the
 * detected agents are migrated (the Initialize step pre-selects them).
 */
export function DetectedPanel({
  detected,
  removed,
  onActionChange,
  replaceCodegraph = [],
  onReplaceCodegraph,
}: {
  detected: Detected;
  removed: readonly string[];
  /** Files whose foreign `codegraph` entry is replaced (`replace_codegraph`). */
  replaceCodegraph?: readonly string[];
  onReplaceCodegraph?: (file: string, on: boolean) => void;
  /** `migrate` selects the agent for HTTP wiring; `remove` strips its entry instead. */
  onActionChange: (agent: string, action: "migrate" | "remove") => void;
}) {
  const { existing_db, managed_hooks, detected_agents, custom_db_paths } = detected;
  const cgEntries = detected.codegraph_entries ?? [];
  const cgSections = detected.codegraph_sections ?? [];
  const cgHooks = detected.codegraph_hooks ?? [];
  if (
    cgEntries.length === 0 &&
    cgSections.length === 0 &&
    cgHooks.length === 0 &&
    !existing_db &&
    managed_hooks.length === 0 &&
    detected_agents.length === 0 &&
    !custom_db_paths.some((p) => p.exists)
  ) {
    return null;
  }
  // The earlier-install wording applies to 1.x leftovers, not to a re-attached history.
  const earlier = !(
    cgEntries.length === 0 &&
    cgSections.length === 0 &&
    cgHooks.length === 0 &&
    managed_hooks.length === 0 &&
    detected_agents.length === 0 &&
    !custom_db_paths.some((p) => p.exists)
  );
  return (
    <section
      aria-label="Existing WhyGraph data"
      data-testid="detected-panel"
      className="flex flex-col gap-3 rounded-lg border border-border p-4"
    >
      <div>
        <h3 className="text-sm font-semibold">Existing WhyGraph data</h3>
        <p className="text-xs text-muted-foreground">
          {earlier
            ? "This repository was set up with an earlier WhyGraph. Adding it is like adding a new project; here is what carries over."
            : "The portal kept this project's history from an earlier add."}
        </p>
      </div>
      <ul className="flex flex-col divide-y divide-border">
        {existing_db && (
          <Item icon={<DatabaseIcon />} title="Existing history">
            <p className="text-xs text-muted-foreground">
              History found in the portal (re-attached). Already-described commits cost nothing to
              scan again.
            </p>
          </Item>
        )}
        {managed_hooks.length > 0 && (
          <Item icon={<GitBranchIcon />} title="Git hooks from an earlier install">
            <p className="text-xs text-muted-foreground">
              <span className="font-mono">{managed_hooks.join(", ")}</span> keep working until you
              initialize; initializing points them at the portal.
            </p>
          </Item>
        )}
        {detected_agents.map((entry) => {
          const info = agentInfo(entry.agent);
          const action = removed.includes(entry.agent) ? "remove" : "migrate";
          return (
            <Item
              key={`${entry.file}:${entry.key}`}
              icon={<PlugIcon />}
              title={`${info?.label ?? entry.agent} entry`}
            >
              <p className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                <span className="font-mono">{entry.file}</span>
                <span className="font-mono">{entry.key}</span>
                <Badge variant="outline">{SHAPE_LABEL[entry.shape]}</Badge>
                {entry.stale && <Badge variant="outline">VS Code never read this one</Badge>}
                {entry.tracked && <Badge variant="secondary">committed to git</Badge>}
              </p>
              <div
                role="group"
                aria-label={`${info?.label ?? entry.agent} entry`}
                className="mt-1 inline-flex w-fit overflow-hidden rounded-md border border-border text-xs"
              >
                {(["migrate", "remove"] as const).map((a) => (
                  <button
                    key={a}
                    type="button"
                    aria-pressed={action === a}
                    onClick={() => onActionChange(entry.agent, a)}
                    className={cn(
                      "px-2.5 py-1 transition-colors",
                      action === a
                        ? "bg-primary text-primary-foreground"
                        : "text-muted-foreground hover:bg-muted",
                    )}
                  >
                    {a === "migrate" ? "Migrate to HTTP" : "Remove entry"}
                  </button>
                ))}
              </div>
            </Item>
          );
        })}
        {cgEntries.map((entry) => (
          <ForeignCodegraphEntry
            key={`${entry.file}:${entry.key}`}
            file={entry.file}
            detail={SHAPE_LABEL[entry.shape]}
            tracked={entry.tracked}
            replace={replaceCodegraph.includes(entry.file)}
            onReplaceChange={(on) => onReplaceCodegraph?.(entry.file, on)}
          />
        ))}
        {cgSections.map((file) => (
          <Item key={`section:${file}`} icon={<TriangleAlertIcon />} title={CODEGRAPH_NOTE}>
            <p className="text-xs text-muted-foreground" data-testid="foreign-codegraph-section">
              <span className="font-mono">{file}</span> has a CodeGraph instruction section. It is left as it is.
            </p>
          </Item>
        ))}
        {cgHooks.map((hook) => (
          <Item key={`hook:${hook}`} icon={<TriangleAlertIcon />} title={CODEGRAPH_NOTE}>
            <p className="text-xs text-muted-foreground" data-testid="foreign-codegraph-hook">
              <span className="font-mono break-all">{hook}</span> has CodeGraph's own git hook. It is left as it is.
            </p>
          </Item>
        ))}
      </ul>
      <CustomDbWarnings paths={custom_db_paths} />
    </section>
  );
}
