import { useMemo, useState, type Ref } from "react";
import { Autocomplete } from "@base-ui/react/autocomplete";
import { Input } from "../ui/input";

type ModelOption = { id: string; display_name: string };

/**
 * The model id field of a settings model row: free text, with the provider's
 * listed models as suggestions once there are some. It replaces a native
 * `<datalist>`, whose popup the browser draws and no CSS reaches.
 *
 * An empty field or one holding a listed id shows the whole list, so a set
 * field can still be browsed; anything else filters by id or display name.
 * Any id is accepted - the list only suggests. The input stays mounted while
 * the listing loads (no swap from a plain input), so typing keeps its focus.
 */
export function ModelIdInput({
  fieldProps,
  value,
  onChange,
  onBlur,
  inputRef,
  models,
  label,
  placeholder,
}: {
  /** The `Field`'s id and `aria-*` wiring. */
  fieldProps: { id: string; "aria-invalid": boolean; "aria-describedby": string | undefined };
  value: string;
  onChange: (value: string) => void;
  onBlur: () => void;
  inputRef: Ref<HTMLInputElement>;
  /** The listed models; without them the list never opens. */
  models: ModelOption[] | undefined;
  label: string;
  placeholder: string;
}) {
  const [open, setOpen] = useState(false);
  const query = value.trim().toLowerCase();
  const filtered = useMemo(() => {
    if (!models || !query || models.some((m) => m.id.toLowerCase() === query)) return models ?? [];
    return models.filter((m) => m.id.toLowerCase().includes(query) || m.display_name.toLowerCase().includes(query));
  }, [models, query]);

  return (
    <Autocomplete.Root
      items={models ?? []}
      filteredItems={filtered}
      itemToStringValue={(m: ModelOption) => m.id}
      value={value}
      onValueChange={(next) => onChange(next)}
      open={open && !!models}
      onOpenChange={setOpen}
      openOnInputClick
    >
      <Autocomplete.Input
        render={
          <Input
            {...fieldProps}
            ref={inputRef}
            onBlur={onBlur}
            aria-label={`${label} model`}
            placeholder={placeholder}
            title={placeholder}
            className="min-w-48 flex-1 font-mono"
          />
        }
      />
      {models && (
        <Autocomplete.Portal>
          <Autocomplete.Positioner sideOffset={4} className="isolate z-50">
            <Autocomplete.Popup className="max-h-[min(18rem,var(--available-height))] w-(--anchor-width) origin-(--transform-origin) overflow-y-auto rounded-lg bg-popover p-1 text-popover-foreground shadow-md ring-1 ring-foreground/10 duration-100 data-[side=bottom]:slide-in-from-top-2 data-[side=top]:slide-in-from-bottom-2 data-open:animate-in data-open:fade-in-0 data-open:zoom-in-95 data-closed:animate-out data-closed:fade-out-0 data-closed:zoom-out-95">
              <Autocomplete.Empty className="px-2.5 py-2 text-xs text-muted-foreground empty:hidden">
                No listed model matches. Any model id works.
              </Autocomplete.Empty>
              <Autocomplete.List>
                {(m: ModelOption) => (
                  <Autocomplete.Item
                    key={m.id}
                    value={m}
                    className="flex cursor-pointer items-baseline justify-between gap-3 rounded-md px-2.5 py-2 text-sm outline-hidden select-none data-highlighted:bg-accent data-highlighted:text-accent-foreground"
                  >
                    <span className="min-w-0 truncate font-mono">{m.id}</span>
                    {m.display_name !== m.id && (
                      <span className="min-w-0 max-w-[45%] truncate text-xs text-muted-foreground">{m.display_name}</span>
                    )}
                  </Autocomplete.Item>
                )}
              </Autocomplete.List>
            </Autocomplete.Popup>
          </Autocomplete.Positioner>
        </Autocomplete.Portal>
      )}
    </Autocomplete.Root>
  );
}
