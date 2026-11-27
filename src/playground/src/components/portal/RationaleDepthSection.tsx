import { useId, type ReactNode } from "react";
import { Controller, type UseFormReturn } from "react-hook-form";
import { InfoIcon } from "lucide-react";
import type { ConfigFormValues } from "../../lib/configForm";
import {
  DEFAULT_CEILING,
  DEFAULT_DEPTH,
  DEPTH_LEVELS,
  depthHint,
  depthOptionName,
  depthShortName,
  rankOf,
} from "../../lib/rationaleDepth";
import { Button } from "../ui/button";
import { Label } from "../ui/label";
import { Popover, PopoverContent, PopoverTrigger } from "../ui/popover";
import { DepthInfoButton } from "./DepthInfo";
import { nativeSelect } from "./Field";

/** A label with its (i) beside it (never inside the `<label>`, so the select's name stays "Depth"). */
function LabelRow({ htmlFor, label, info }: { htmlFor: string; label: string; info?: ReactNode }) {
  return (
    <div className="flex items-center gap-1">
      <Label htmlFor={htmlFor}>{label}</Label>
      {info}
    </div>
  );
}

function CeilingInfo({ orgCeiling }: { orgCeiling: string | null }) {
  return (
    <Popover>
      <PopoverTrigger
        openOnHover
        delay={150}
        closeDelay={120}
        render={<Button type="button" variant="ghost" size="icon-xs" aria-label="What the ceiling does" />}
      >
        <InfoIcon />
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80">
        <p>
          The deepest level that Auto, an agent, or the Generate deeper button may use for this project. Cards above it
          are never generated, whatever an agent asks for.
          {orgCeiling ? ` Your organization's ceiling: ${depthShortName(orgCeiling)}.` : ""}
        </p>
      </PopoverContent>
    </Popover>
  );
}

/**
 * The project's "Rationale depth" fields: the Depth and Ceiling selects (native, like
 * every dropdown here), each with a hint that follows the choice. What the form holds
 * is the project layer's own value; `""` shows what the project inherits.
 *
 * `orgCeiling` is the organization's ceiling in production (`null` in local mode, which
 * has none); a ceiling above it is disabled, and a stored one above it says it is capped.
 */
export function RationaleDepthFields({
  form,
  readOnly,
  inheritedDepth,
  inheritedMax,
  orgCeiling,
}: {
  form: UseFormReturn<ConfigFormValues>;
  readOnly: boolean;
  inheritedDepth: string;
  inheritedMax: string;
  orgCeiling: string | null;
}) {
  const depthId = useId();
  const maxId = useId();
  const { control, formState } = form;
  const error = formState.errors.maxDepth?.message;
  return (
    <>
      <Controller
        control={control}
        name="maxDepth"
        render={({ field: ceilingField }) => {
          const ceiling = ceilingField.value || inheritedMax;
          const capped = orgCeiling !== null && rankOf(ceiling) > rankOf(orgCeiling);
          return (
            <>
              <Controller
                control={control}
                name="depth"
                render={({ field }) => {
                  const value = field.value || inheritedDepth;
                  return (
                    <div className="flex flex-col gap-1.5">
                      <LabelRow htmlFor={depthId} label="Depth" info={<DepthInfoButton />} />
                      <select
                        id={depthId}
                        className={nativeSelect()}
                        value={value}
                        disabled={readOnly}
                        aria-describedby={`${depthId}-hint`}
                        onChange={(e) => field.onChange(e.target.value)}
                      >
                        {[DEFAULT_DEPTH, ...DEPTH_LEVELS].map((option) => {
                          const above = option !== "auto" && rankOf(option) > rankOf(ceiling);
                          return (
                            <option key={option} value={option} disabled={above && option !== value}>
                              {depthOptionName(option)}
                              {above ? " (above ceiling)" : ""}
                            </option>
                          );
                        })}
                      </select>
                      <p id={`${depthId}-hint`} className="text-xs text-muted-foreground">
                        {depthHint(value)}
                      </p>
                    </div>
                  );
                }}
              />
              <div className="flex flex-col gap-1.5">
                <LabelRow htmlFor={maxId} label="Ceiling" info={<CeilingInfo orgCeiling={orgCeiling} />} />
                <select
                  id={maxId}
                  className={nativeSelect()}
                  value={ceiling}
                  disabled={readOnly}
                  aria-invalid={!!error}
                  aria-describedby={`${maxId}-hint`}
                  onChange={(e) => ceilingField.onChange(e.target.value)}
                >
                  {DEPTH_LEVELS.map((option) => {
                    const above = orgCeiling !== null && rankOf(option) > rankOf(orgCeiling);
                    return (
                      <option key={option} value={option} disabled={above && option !== ceiling}>
                        {depthOptionName(option)}
                        {above ? " (above your organization's ceiling)" : ""}
                      </option>
                    );
                  })}
                </select>
                {error ? (
                  <p id={`${maxId}-hint`} role="alert" className="text-xs text-destructive">
                    {error}
                  </p>
                ) : (
                  <p id={`${maxId}-hint`} className="text-xs text-muted-foreground">
                    Auto, agents and "Generate deeper" never go above this.
                    {orgCeiling ? ` Your organization allows up to ${depthShortName(orgCeiling)}.` : ""}
                    {capped ? ` Capped by your organization at ${depthShortName(orgCeiling)}.` : ""}
                  </p>
                )}
              </div>
            </>
          );
        }}
      />
    </>
  );
}

/** The organization's ceiling (production, org defaults): the one select, with its hint. */
export function OrgCeilingField({ form, readOnly }: { form: UseFormReturn<ConfigFormValues>; readOnly: boolean }) {
  const id = useId();
  return (
    <Controller
      control={form.control}
      name="orgMaxDepth"
      render={({ field }) => (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={id}>Organization ceiling</Label>
          <select
            id={id}
            className={nativeSelect()}
            value={field.value || DEFAULT_CEILING}
            disabled={readOnly}
            aria-describedby={`${id}-hint`}
            onChange={(e) => field.onChange(e.target.value)}
          >
            {DEPTH_LEVELS.map((option) => (
              <option key={option} value={option}>
                {depthOptionName(option)}
              </option>
            ))}
          </select>
          <p id={`${id}-hint`} className="text-xs text-muted-foreground">
            Projects can set a lower ceiling, never a higher one. Lowering it never rewrites a project: its cards are capped
            at this level and its page says so.
          </p>
        </div>
      )}
    />
  );
}
