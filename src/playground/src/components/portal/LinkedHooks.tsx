import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { projectApi, projectKey, type ConfigDict } from "../../api";
import { HOOK_NAMES, hooksFromLayer, hooksToValue, serveCodegraphOn, type HookName } from "../../lib/configForm";
import { useReadOnly } from "../../lib/identity";
import { Checkbox } from "../ui/checkbox";
import { Label } from "../ui/label";
import { Switch } from "../ui/switch";
import { Skeleton } from "../ui/skeleton";
import { SectionForm } from "../settings/SectionForm";

/**
 * Two settings a linked project still keeps on this machine: which git hooks
 * rescan the checkout (`[scan].hooks`) and whether the portal serves CodeGraph
 * tools for it (`LinkedCodegraph`). Everything else in its config is the
 * platform's, so the full config form is not shown. The save sends
 * `[scan].hooks` and `[scan].serve_codegraph` alone: a linked project's PUT
 * allowlist holds nothing else, and the portal refuses a layer carrying another
 * key rather than dropping it.
 */
export function LinkedHooks({ slug, readOnly: readOnlyProp = false }: { slug: string; readOnly?: boolean }) {
  const queryClient = useQueryClient();
  const readOnly = useReadOnly() || readOnlyProp;
  const config = useQuery({ queryKey: projectKey(slug, "config"), queryFn: () => projectApi(slug).config() });
  const [picked, setPicked] = useState<Record<HookName, boolean> | null>(null);
  const layer: ConfigDict = config.data?.config ?? {};
  const saved = hooksFromLayer(layer);
  const hooks = picked ?? saved;
  const dirty = HOOK_NAMES.some((h) => hooks[h] !== saved[h]);

  const save = useMutation({
    mutationFn: () => {
      // [scan].hooks and [scan].serve_codegraph alone: a linked project's PUT
      // allowlist holds nothing else, and the portal refuses a layer that
      // carries any other key rather than dropping it.
      const value = hooksToValue(hooks);
      const scan = {
        ...(value === undefined ? {} : { hooks: value }),
        // The other writable key (see LinkedCodegraph): a save of one keeps the other.
        ...(serveCodegraphOn(layer) ? {} : { serve_codegraph: false }),
      };
      return projectApi(slug).putConfig({ config: { scan } });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: projectKey(slug, "config") });
      setPicked(null);
    },
  });

  if (config.isLoading) return <Skeleton className="h-16" />;
  return (
    <SectionForm
      name="Git hooks"
      dirty={dirty}
      readOnly={readOnly}
      onSave={() => save.mutateAsync()}
      onDiscard={() => setPicked(null)}
    >
      <div className="grid gap-2 sm:grid-cols-2">
        {HOOK_NAMES.map((hook) => (
          <label key={hook} className="flex cursor-pointer items-center gap-2 text-sm">
            <Checkbox
              checked={hooks[hook]}
              disabled={readOnly}
              onCheckedChange={(on) => setPicked({ ...hooks, [hook]: on === true })}
            />
            <span className="font-mono text-[13px]">{hook}</span>
          </label>
        ))}
      </div>
    </SectionForm>
  );
}

/**
 * A linked project's `[scan].serve_codegraph` switch. The save sends the saved
 * hooks along (a PUT replaces the layer) so it never changes them.
 */
export function LinkedCodegraph({ slug, readOnly: readOnlyProp = false }: { slug: string; readOnly?: boolean }) {
  const queryClient = useQueryClient();
  const readOnly = useReadOnly() || readOnlyProp;
  const config = useQuery({ queryKey: projectKey(slug, "config"), queryFn: () => projectApi(slug).config() });
  const [picked, setPicked] = useState<boolean | null>(null);
  const layer: ConfigDict = config.data?.config ?? {};
  const saved = serveCodegraphOn(layer);
  const on = picked ?? saved;

  const save = useMutation({
    mutationFn: () => {
      const hooks = hooksToValue(hooksFromLayer(layer));
      const scan = {
        ...(hooks === undefined ? {} : { hooks }),
        ...(on ? {} : { serve_codegraph: false }),
      };
      return projectApi(slug).putConfig({ config: { scan } });
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: projectKey(slug, "config") });
      await queryClient.invalidateQueries({ queryKey: projectKey(slug, "project-overview") });
      setPicked(null);
    },
  });

  if (config.isLoading) return <Skeleton className="h-10" />;
  return (
    <SectionForm
      name="Scanning"
      dirty={on !== saved}
      readOnly={readOnly}
      onSave={() => save.mutateAsync()}
      onDiscard={() => setPicked(null)}
    >
      <div className="row-wrap justify-between gap-3">
        <Label htmlFor="linked-serve-codegraph" className="cursor-pointer">Serve CodeGraph tools to agents</Label>
        <Switch id="linked-serve-codegraph" checked={on} disabled={readOnly} onCheckedChange={(v) => setPicked(v)} />
      </div>
    </SectionForm>
  );
}
