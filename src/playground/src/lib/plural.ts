import { formatNumber } from "./format";

/**
 * A count with its noun: `plural(1, "message")` -> "1 message", `plural(3,
 * "message")` -> "3 messages", `plural(1240, "commit")` -> "1,240 commits". Pass
 * `many` for a noun whose plural is not "+s" (`plural(2, "entry", "entries")`).
 */
export function plural(n: number, one: string, many: string = `${one}s`): string {
  return `${formatNumber(n)} ${n === 1 ? one : many}`;
}
