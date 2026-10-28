import type { ChatModeInfo } from "../../api";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../ui/select";

// The chat's mode, beside the model pickers. A session always has one, so there is no "default"
// entry; a stored id this list does not know (a newer server) stays selectable as itself.

export function ModeSelect({
  value,
  modes,
  onChange,
  disabled,
}: {
  value: string;
  modes: ChatModeInfo[];
  onChange: (next: string) => void;
  disabled?: boolean;
}) {
  const items = modes.map((m) => ({ value: m.id, label: m.label }));
  if (!items.some((i) => i.value === value)) items.push({ value, label: value });
  const description = modes.find((m) => m.id === value)?.description;
  return (
    <Select
      items={items}
      value={value}
      disabled={disabled}
      onValueChange={(next) => {
        if (next) onChange(next);
      }}
    >
      <SelectTrigger
        aria-label="Mode"
        title={description ?? "How the assistant approaches the question."}
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
