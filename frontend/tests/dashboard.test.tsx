import React from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import Dashboard from "../components/dashboard";
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
test("initial state is honest and workflow modules are linked", () => {
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  render(<Dashboard />);
  expect(screen.getByRole("heading", { name: "Workspace overview" })).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Checking API");
  expect(screen.getByRole("link", { name: "Connections" })).toHaveAttribute("href", "/connections");
  expect(screen.getByRole("link", { name: "Workflows" })).toHaveAttribute("href", "/workflows");
  expect(screen.getByRole("link", { name: "Alerts" })).toHaveAttribute("href", "/alerts");
  expect(screen.getByText(/Durable workflows and execution history are available locally/)).toBeInTheDocument();
});
test("successful health response updates connectivity", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ status: "healthy" }) }));
  render(<Dashboard />);
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("API connected"));
});
test("network failure gives a retryable error", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
  render(<Dashboard />);
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("API unavailable"));
  expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
});
