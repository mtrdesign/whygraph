import { useEffect } from "react";
import { useNavigate, useParams } from "@tanstack/react-router";
import { useUi } from "../../store";

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

/**
 * Global keys (§4.12.4): ⌘K / Ctrl-K opens the command menu anywhere, and
 * `g` then a letter jumps to a page (`g p` projects, and inside a project `g o`
 * overview, `g e` explorer, `g c` chat, `g s` scans). Ignored while typing.
 */
export function useGlobalShortcuts() {
  const navigate = useNavigate();
  const { slug } = useParams({ strict: false }) as { slug?: string };
  const setPaletteOpen = useUi((s) => s.setPaletteOpen);

  useEffect(() => {
    let armed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const disarm = () => {
      armed = false;
      clearTimeout(timer);
    };

    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen(true);
        return;
      }
      if (e.metaKey || e.ctrlKey || e.altKey || isTyping(e.target)) return;

      const key = e.key.toLowerCase();
      if (!armed) {
        if (key === "g") {
          armed = true;
          timer = setTimeout(disarm, 1000);
        }
        return;
      }
      disarm();
      if (key === "p") navigate({ to: "/" });
      else if (slug && key === "o") navigate({ to: "/p/$slug", params: { slug } });
      else if (slug && key === "e") navigate({ to: "/p/$slug/explorer", params: { slug } });
      else if (slug && key === "c") navigate({ to: "/p/$slug/chat/{-$id}", params: { slug } });
      else if (slug && key === "s") navigate({ to: "/p/$slug/scans/{-$runId}", params: { slug } });
    };

    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      clearTimeout(timer);
    };
  }, [navigate, slug, setPaletteOpen]);
}
