import { useState, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { CheckIcon, FolderGit2Icon } from "lucide-react";
import type { OnboardingItemId, ProjectSummary } from "../../api";
import { cn } from "../../lib/utils";
import { Button } from "../ui/button";
import { ConnectAgentDialog } from "./ConnectAgentDialog";
import type { FirstRun } from "./firstRun";

type Item = FirstRun["items"][number];

interface Copy {
  title: string;
  why: string;
  action: ReactNode;
}

/**
 * The first-run checklist (ONB-1): the steps that make a new org or a fresh local
 * portal useful, over `GET /api/onboarding`. Each item has a check or a number, a
 * title, one line of why and one action. With no projects it is the empty state.
 */
export function FirstRunChecklist({
  firstRun,
  production,
  projects,
  githubLogin,
  empty,
}: {
  firstRun: FirstRun;
  production: boolean;
  projects: ProjectSummary[];
  /** The caller's GitHub login; `null` for a password account (the bootstrap owner). */
  githubLogin: string | null | undefined;
  empty: boolean;
}) {
  const [agentOpen, setAgentOpen] = useState(false);
  const { items } = firstRun;
  const hasLlmItem = items.some((i) => i.id === "llm_key");
  const first = projects[0];

  const copy = (item: Item): Copy => {
    const link = (label: string, to: string, extra: { search?: object; params?: object } = {}) => (
      <Button
        size="sm"
        variant="outline"
        render={<Link to={to as "/"} search={extra.search as never} params={extra.params as never} />}
      >
        {label}
      </Button>
    );
    const byId: Record<OnboardingItemId, () => Copy> = {
      github: () => ({
        title: "Connect GitHub",
        why: "WhyGraph imports repositories through the WhyGraph GitHub App.",
        action: item.done ? null : !githubLogin ? (
          <span className="text-sm text-muted-foreground">Sign in with GitHub to import repositories</span>
        ) : item.can_act ? (
          link("Install the WhyGraph GitHub App", "/projects/new")
        ) : null,
      }),
      project: () => ({
        title: production ? "Import a repository" : "Add a project",
        why: production
          ? "WhyGraph indexes its history; the first scan is free."
          : "Pick a repository from a shared folder; the first scan is free.",
        action: link(production ? "Import a repository" : "Add project", "/projects/new"),
      }),
      llm_key: () => ({
        title: "Add an LLM key",
        why: "Needed for descriptions, rationale cards and Chat.",
        action: item.can_act ? (
          link("Add a key", "/settings", { search: { section: "models" } })
        ) : (
          <span className="text-sm text-muted-foreground">Ask an owner</span>
        ),
      }),
      invite: () => ({
        title: "Invite your team",
        why: "Members read the history and ask their agents about it.",
        action: link("Invite", "/members"),
      }),
      agent: () => ({
        title: "Connect your agent",
        why: "Your coding agent then reads the history while it works.",
        action: production ? (
          <Button size="sm" variant="outline" onClick={() => setAgentOpen(true)}>
            Connect your agent
          </Button>
        ) : first ? (
          link("Open the Overview", "/p/$slug", { params: { slug: first.slug } })
        ) : (
          link("Add a project first", "/projects/new")
        ),
      }),
    };
    return byId[item.id]();
  };

  const list = (
    <ol className="flex flex-col divide-y divide-border text-left" data-testid="first-run-list">
      {items.map((item, i) => {
        const c = copy(item);
        return (
          <li
            key={item.id}
            data-testid={`first-run-${item.id}`}
            data-done={item.done ? "true" : "false"}
            className="flex flex-wrap items-center gap-3 py-3"
          >
            <span
              className={cn(
                "flex size-6 shrink-0 items-center justify-center rounded-full text-xs font-medium",
                item.done ? "bg-success-soft text-success" : "bg-muted text-muted-foreground",
              )}
              aria-hidden
            >
              {item.done ? <CheckIcon className="size-3.5" /> : i + 1}
            </span>
            <div className="flex min-w-0 flex-1 flex-col">
              <span className={cn("text-sm font-medium", item.done && "text-muted-foreground")}>
                {c.title}
                {item.done && <span className="sr-only"> (done)</span>}
              </span>
              <span className="text-xs text-muted-foreground">{c.why}</span>
            </div>
            {!item.done && c.action}
          </li>
        );
      })}
    </ol>
  );

  return (
    <section
      className="flex flex-col gap-3 rounded-xl bg-card p-5 shadow-card"
      data-testid="first-run-checklist"
      aria-labelledby="first-run-title"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 items-start gap-3">
          {empty && <FolderGit2Icon className="mt-0.5 size-5 shrink-0 text-muted-foreground" aria-hidden />}
          <div>
            <h2 id="first-run-title" className="text-sm font-semibold">
              {empty ? "No projects yet" : "Getting started"}
            </h2>
            <p className="text-xs text-muted-foreground">
              {empty ? "A few short steps and WhyGraph is working for you." : "What is left to set up."}
            </p>
          </div>
        </div>
        <Button size="sm" variant="ghost" onClick={firstRun.dismiss}>
          Dismiss
        </Button>
      </div>
      {list}
      {!hasLlmItem && projects.length > 0 && (
        <p className="text-xs text-muted-foreground" data-testid="first-run-linked-note">
          Linked projects use the platform's keys.
        </p>
      )}
      <ConnectAgentDialog open={agentOpen} onOpenChange={setAgentOpen} />
    </section>
  );
}
