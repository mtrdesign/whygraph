import { useTheme, type Theme } from "../theme";

const ORDER: Theme[] = ["system", "light", "dark"];
const LABEL: Record<Theme, string> = { system: "System", light: "Light", dark: "Dark" };

// Cycles system -> light -> dark. The sidebar footer and the command menu get
// their own entry points with the app shell (step 11b); until then it lives in
// the header so both themes can be reached.
export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const next = ORDER[(ORDER.indexOf(theme) + 1) % ORDER.length];
  return (
    <button
      type="button"
      onClick={() => setTheme(next)}
      title={`Theme: ${LABEL[theme]} (click for ${LABEL[next]})`}
      aria-label={`Theme: ${LABEL[theme]}. Switch to ${LABEL[next]}`}
      className="flex items-center gap-2 rounded-md border border-border bg-panel2 px-3 py-1 text-xs text-muted-foreground hover:text-fg"
    >
      {LABEL[theme]}
    </button>
  );
}
