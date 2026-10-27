import { EFFORT_LEVELS } from "../../lib/configForm";
import { effortLabel } from "../../lib/labels";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select";

// The chat's effort level, beside the model pickers. `value` is the session's own level, or
// `null` for "use the default"; `defaultEffort` is what that default is right now (the Chat
// default from Settings, `null` when it is unset and the provider decides).

const DEFAULT = "default";

export function EffortSelect({
  value,
  defaultEffort,
  onChange,
  disabled,
}: {
  value: string | null;
  defaultEffort: string | null;
  /** The new level, or `null` to go back to the default. */
  onChange: (next: string | null) => void;
  disabled?: boolean;
}) {
  const items = [
    { value: DEFAULT, label: `Default (${defaultEffort ? effortLabel(defaultEffort) : "provider default"})` },
    ...EFFORT_LEVELS.map((level) => ({ value: level as string, label: effortLabel(level) })),
  ];
  return (
    <Select
      items={items}
      value={value ?? DEFAULT}
      disabled={disabled}
      onValueChange={(next) => {
        if (next) onChange(next === DEFAULT ? null : next);
      }}
    >
      <SelectTrigger
        aria-label="Effort"
        title="How hard the model thinks. Default uses the level set in Settings."
        size="sm"
        className="w-auto"
      >
        <SelectValue />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false} className="w-auto">
        {items.map((item) => (
          <SelectItem key={item.value} value={item.value}>
            {item.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
