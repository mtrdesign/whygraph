import { cloneElement, isValidElement, useEffect, useId, useState, type ReactElement, type ReactNode } from "react";
import { Tooltip, TooltipContent, TooltipTrigger } from "../ui/tooltip";

const TOUCH_QUERY = "(hover: none)";

function touchNow(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia(TOUCH_QUERY).matches
    : false;
}

/** Whether the device has no hover (a phone or a tablet), so a tooltip would never show. */
function useIsTouch(): boolean {
  const [touch, setTouch] = useState(touchNow);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const media = window.matchMedia(TOUCH_QUERY);
    const change = () => setTouch(media.matches);
    media.addEventListener?.("change", change);
    return () => media.removeEventListener?.("change", change);
  }, []);
  return touch;
}

/**
 * Why a control is disabled (M2f-3 CN-6). On a touch device the reason is plain
 * text beside the control (a tooltip needs hover); on desktop it is a tooltip,
 * and the control is described by the reason either way (`aria-describedby`).
 */
export function DisabledReason({ reason, children }: { reason: ReactNode; children: ReactNode }) {
  const id = useId();
  const touch = useIsTouch();
  const described = isValidElement(children)
    ? cloneElement(children as ReactElement<{ "aria-describedby"?: string }>, { "aria-describedby": id })
    : children;

  if (touch) {
    return (
      <div className="flex flex-col items-start gap-1" data-testid="disabled-reason">
        {described}
        <p id={id} className="text-xs text-muted-foreground">
          {reason}
        </p>
      </div>
    );
  }
  return (
    <>
      <Tooltip>
        <TooltipTrigger render={<span className="inline-flex" tabIndex={0} data-testid="disabled-reason" />}>
          {described}
        </TooltipTrigger>
        <TooltipContent>{reason}</TooltipContent>
      </Tooltip>
      <span id={id} className="sr-only">
        {reason}
      </span>
    </>
  );
}
