import { create } from "zustand";

interface LiveState {
  text: string;
  tick: number;
}

const useLive = create<LiveState>(() => ({ text: "", tick: 0 }));

/** Say `text` to screen readers (polite). The same text twice in a row is still read. */
export function announce(text: string): void {
  useLive.setState((s) => ({ text, tick: s.tick + 1 }));
}

/** The one polite live region, mounted once by the app shell. */
export function LiveRegion() {
  const { text, tick } = useLive();
  return (
    <div role="status" aria-live="polite" aria-atomic="true" className="sr-only">
      {text ? text + (tick % 2 ? "" : " ") : ""}
    </div>
  );
}
