/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // A restrained slate/indigo "platform" palette (Linear/Vercel-ish).
        bg: "#0b0d12",
        panel: "#12151c",
        panel2: "#171b24",
        border: "#242a36",
        muted: "#8b93a7",
        fg: "#e6e9f0",
        accent: "#6366f1",
        accent2: "#818cf8",
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};
