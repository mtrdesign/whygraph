import { nativeSelect } from "../portal/Field";

/**
 * A `<select>` stand-in for a long tab strip below `sm` (the caller hides the
 * strip itself at that width).
 */
export function TabsSelect({
  value,
  onValueChange,
  tabs,
  label,
}: {
  value: string;
  onValueChange: (value: string) => void;
  tabs: { value: string; label: string }[];
  label: string;
}) {
  return (
    <select
      aria-label={label}
      className={nativeSelect("sm:hidden")}
      value={value}
      onChange={(e) => onValueChange(e.target.value)}
    >
      {tabs.map((t) => (
        <option key={t.value} value={t.value}>
          {t.label}
        </option>
      ))}
    </select>
  );
}
