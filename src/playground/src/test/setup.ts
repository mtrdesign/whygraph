import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// Vitest globals are off, so Testing Library's automatic cleanup is not wired.
afterEach(() => {
  cleanup();
});

// jsdom lacks these; cmdk (the command menu) and Base UI popups use them.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver ??= ResizeObserverStub;
Element.prototype.scrollIntoView ??= () => {};
// Base UI's scroll area asks for running animations when it settles.
Element.prototype.getAnimations ??= () => [];

// jsdom cannot navigate; every full-page redirect goes through this one function.
vi.mock("../lib/navigation", () => ({
  hardNavigate: vi.fn(() => new Promise<never>(() => {})),
}));
