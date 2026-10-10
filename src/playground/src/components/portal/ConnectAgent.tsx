import { AGENTS, mcpSnippet } from "../../lib/agents";
import { Badge } from "../ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../ui/tabs";
import { CopyButton } from "./CopyButton";

/**
 * "Connect your agent" (screen 9a): the project's MCP URL with a copy button, and
 * per agent the entry that Initialize writes, for pasting into a config by hand.
 * The badge says which agents this portal already configured.
 */
export function ConnectAgent({ mcpUrl, configured }: { mcpUrl: string; configured: readonly string[] }) {
  return (
    <section className="flex flex-col gap-3 rounded-xl border border-border bg-card p-5" data-testid="connect-agent">
      <div>
        <h2 className="text-sm font-semibold">Connect your agent</h2>
        <p className="text-xs text-muted-foreground">
          Agents reach this project over HTTP. Initialize writes the entry below; paste it yourself if
          you set an agent up by hand.
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <code
          data-testid="mcp-url"
          className="min-w-0 flex-1 rounded-md bg-muted px-2 py-1.5 font-mono text-xs break-all"
        >
          {mcpUrl}
        </code>
        <CopyButton text={mcpUrl} />
      </div>
      <Tabs defaultValue={configured[0] ?? AGENTS[0].id}>
        <TabsList variant="scrollable" className="max-w-full">
          {AGENTS.map((a) => (
            <TabsTrigger key={a.id} value={a.id}>
              {a.label}
            </TabsTrigger>
          ))}
        </TabsList>
        {AGENTS.map((a) => {
          const snippet = mcpSnippet(a.id, mcpUrl);
          return (
            <TabsContent key={a.id} value={a.id} className="mt-3 flex flex-col gap-2">
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <span className="font-mono">{a.file}</span>
                {configured.includes(a.id) ? (
                  <Badge variant="secondary">configured</Badge>
                ) : (
                  <Badge variant="outline">not configured</Badge>
                )}
              </div>
              <pre data-scroll-x className="max-h-56 overflow-auto rounded-md bg-muted p-3 font-mono text-xs">
                {snippet}
              </pre>
              <div>
                <CopyButton text={snippet} label="Copy snippet" />
              </div>
              {a.note && <p className="text-xs text-muted-foreground">{a.note}</p>}
            </TabsContent>
          );
        })}
      </Tabs>
    </section>
  );
}
