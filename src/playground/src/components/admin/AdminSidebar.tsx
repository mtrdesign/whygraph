import { Link } from "@tanstack/react-router";
import { useMutation } from "@tanstack/react-query";
import { ArrowLeftIcon, LogOutIcon, NetworkIcon, SearchIcon } from "lucide-react";
import { adminApi, type AdminMe } from "../../api";
import { formatDateTime } from "../../lib/format";
import { hardNavigate } from "../../lib/navigation";
import { useUi } from "../../store";
import { cn } from "@/lib/utils";
import { UserAvatar } from "../auth/UserAvatar";
import { NavLink, Section } from "../shell/Sidebar";
import { ThemeToggle } from "../ThemeToggle";
import { ADMIN_NAV } from "./adminNav";

const ITEM =
  "flex h-8 items-center gap-2.5 rounded-md px-2 text-muted-foreground outline-none transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:ring-2 focus-visible:ring-ring";

/**
 * The admin sidebar (plan 59, §4.14): the one "Administration" section, then the
 * footer - Search (the only way to open the palette on a phone), the theme, the
 * signed-in admin with their session's end, a way back to WhyGraph (not for a
 * password account, which has no everyday session) and Sign out.
 */
export function AdminSidebar({ me, inSheet = false }: { me: AdminMe; inSheet?: boolean }) {
  const setPaletteOpen = useUi((s) => s.setPaletteOpen);
  const setNavOpen = useUi((s) => s.setNavOpen);
  const logout = useMutation({
    mutationFn: adminApi.logout,
    onSuccess: ({ redirect }) => void hardNavigate(redirect),
  });
  const who = me.github_login ? `@${me.github_login}` : null;
  return (
    <nav
      aria-label="Main"
      className="flex h-full w-60 shrink-0 flex-col overflow-y-auto border-r border-sidebar-border bg-sidebar text-sidebar-foreground"
    >
      <div className="flex shrink-0 flex-col gap-3 px-3 pb-3 pt-4">
        <div className={cn("flex items-center gap-2 px-1.5", inSheet && "pr-9")}>
          <div className="flex size-[22px] items-center justify-center rounded-md bg-primary">
            <NetworkIcon className="size-3.5 text-primary-foreground" aria-hidden />
          </div>
          <span className="font-semibold tracking-tight">WhyGraph</span>
        </div>
      </div>

      <div className="flex shrink-0 flex-col gap-0.5 px-3 py-1">
        <Section title="Administration">
          {ADMIN_NAV.map((item) => (
            <NavLink key={item.label} item={item} />
          ))}
        </Section>
      </div>

      <div className="flex-1" />

      <div className="flex shrink-0 flex-col gap-1 border-t border-sidebar-border p-3">
        <button
          type="button"
          onClick={() => {
            setNavOpen(false);
            setPaletteOpen(true);
          }}
          className={cn(ITEM, "w-full cursor-pointer text-left")}
        >
          <SearchIcon className="size-4" />
          <span className="flex-1">Search</span>
          <kbd className="rounded border border-border bg-background px-1.5 py-px font-mono text-[11px]">⌘K</kbd>
        </button>
        <ThemeToggle />
        <Link
          to="/admin/account"
          aria-label="Account"
          onClick={() => setNavOpen(false)}
          className="flex items-center gap-2.5 rounded-md px-2 pb-0.5 pt-2 text-left outline-none hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring"
        >
          <UserAvatar name={me.display_name} url={me.avatar_url} className="size-6" />
          <span className="flex min-w-0 flex-1 flex-col leading-tight">
            <span className="truncate text-[13px] font-medium">{me.display_name}</span>
            {who && (
              <span className="truncate text-[11px] text-muted-foreground" title={who}>
                {who}
              </span>
            )}
          </span>
        </Link>
        <p className="px-2 text-[11px] text-muted-foreground" data-testid="session-ends">
          Session ends {formatDateTime(me.session.expires_at)}
        </p>
        {!me.has_password && (
          <Link to="/orgs" onClick={() => setNavOpen(false)} className={ITEM}>
            <ArrowLeftIcon className="size-4" />
            Back to WhyGraph
          </Link>
        )}
        <button
          type="button"
          disabled={logout.isPending}
          onClick={() => logout.mutate()}
          className={cn(ITEM, "w-full cursor-pointer text-left")}
        >
          <LogOutIcon className="size-4" />
          Sign out
        </button>
      </div>
    </nav>
  );
}
