import { MoonIcon, MonitorIcon, SunIcon } from "lucide-react";
import { useTheme, type Theme } from "../theme";

const ORDER: Theme[] = ["system", "light", "dark"];
const LABEL: Record<Theme, string> = { system: "System", light: "Light", dark: "Dark" };
const ICON = { system: MonitorIcon, light: SunIcon, dark: MoonIcon };

// Cycles system -> light -> dark. Lives in the sidebar footer (§4.12.4); the
// command menu offers the same choice by name.
export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const next = ORDER[(ORDER.indexOf(theme) + 1) % ORDER.length];
  const Icon = ICON[theme];
  return (
    <button
      type="button"
      onClick={() => setTheme(next)}
      title={`Theme: ${LABEL[theme]} (click for ${LABEL[next]})`}
      aria-label={`Theme: ${LABEL[theme]}. Switch to ${LABEL[next]}`}
      className="flex h-8 w-full cursor-pointer items-center gap-2.5 rounded-md px-2 text-left text-muted-foreground outline-none transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:ring-2 focus-visible:ring-ring"
    >
      <Icon className="size-4" aria-hidden />
      <span className="flex-1">Theme</span>
      <span className="text-xs">{LABEL[theme]}</span>
    </button>
  );
}
