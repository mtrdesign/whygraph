import { useState } from "react";
import { localLinkUrl, parsePort, readStoredPort, storePort } from "../../lib/connections";
import { Button } from "../ui/button";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { CopyButton } from "./CopyButton";

/**
 * "Use with your agent" (production project home, M2e plan section 4.6): link a
 * checkout on the reader's own machine to this project, so their coding agent
 * gets its evidence. The button is a plain link to the *local* portal's `/link`
 * page - this page never calls the local portal (no cross-origin probe), it only
 * hands over where to go. The port is remembered in `localStorage`, best effort.
 */
export function UseWithAgent({ baseUrl, org, slug }: { baseUrl: string; org: string; slug: string }) {
  const [port, setPort] = useState(readStoredPort);
  const valid = parsePort(port);
  const origin = new URL(baseUrl).origin;
  const href = valid === null ? undefined : localLinkUrl(valid, origin, org, slug);

  return (
    <section
      className="flex flex-col gap-3 rounded-xl border border-border bg-card p-5"
      data-testid="use-with-agent"
    >
      <div>
        <h2 className="text-sm font-semibold">Use with your agent</h2>
        <p className="text-xs text-muted-foreground">
          Link a checkout of this repository on your machine to this project. Your agent then reads
          this project's history through your local WhyGraph, while your uncommitted changes stay on
          your machine.
        </p>
      </div>
      <ol className="flex list-decimal flex-col gap-1.5 pl-5 text-sm">
        <li>
          Install WhyGraph on your machine - see the{" "}
          <a
            href="https://mtrdesign.github.io/whygraph/getting-started/installation/"
            target="_blank"
            rel="noopener noreferrer"
            className="text-primary-text hover:underline"
          >
            installation guide
          </a>
          .
        </li>
        <li className="flex flex-wrap items-center gap-2">
          Start it: <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">whygraph up</code>
          <CopyButton text="whygraph up" />
        </li>
        <li>Open the link below, pick your checkout, and connect.</li>
      </ol>
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="local-port">Your local portal's port</Label>
          <Input
            id="local-port"
            inputMode="numeric"
            value={port}
            aria-invalid={valid === null}
            className="w-28 font-mono"
            onChange={(e) => {
              setPort(e.target.value);
              if (parsePort(e.target.value) !== null) storePort(e.target.value.trim());
            }}
          />
        </div>
        {href ? (
          <Button render={<a href={href} target="_blank" rel="noopener noreferrer" />}>
            Open in my local WhyGraph
          </Button>
        ) : (
          <Button disabled>Open in my local WhyGraph</Button>
        )}
      </div>
      {valid === null && <p className="text-xs text-destructive">Enter a port from 1 to 65535.</p>}
      <p className="text-xs text-muted-foreground">
        Nothing opened? Start it with <span className="font-mono">whygraph up</span> and try again.
        This page does not check whether your local portal is running.
      </p>
    </section>
  );
}
