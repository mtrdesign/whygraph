// OKLCH -> sRGB hex, for tests that check the hand-resolved chart palettes still
// match the OKLCH tokens in styles/theme.css (zrender cannot parse OKLCH).

export function oklchToHex(L: number, C: number, hueDeg: number): string {
  const a = C * Math.cos((hueDeg * Math.PI) / 180);
  const b = C * Math.sin((hueDeg * Math.PI) / 180);
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3;
  const linear = [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ];
  const encode = (x: number) => {
    const c = Math.min(1, Math.max(0, x));
    return c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055;
  };
  return (
    "#" +
    linear
      .map((x) =>
        Math.round(encode(x) * 255)
          .toString(16)
          .padStart(2, "0"),
      )
      .join("")
  );
}

/** Parse `oklch(L C H)` into hex. Throws on anything else. */
export function parseOklch(value: string): string {
  const match = /^oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\)$/.exec(value.trim());
  if (!match) throw new Error(`not an oklch() colour: ${value}`);
  return oklchToHex(Number(match[1]), Number(match[2]), Number(match[3]));
}
