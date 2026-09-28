import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { askResult, fakeApi } from "./fixtures";

function renderAt(path = "/", api = fakeApi()) {
  return { api, ...render(<MemoryRouter initialEntries={[path]}><App api={api} /></MemoryRouter>) };
}

describe("Public Preview product UX", () => {
  beforeEach(() => window.localStorage.clear());
  afterEach(() => { cleanup(); window.localStorage.clear(); vi.restoreAllMocks(); });

  it("uses the browser language on first run", async () => {
    vi.spyOn(window.navigator, "language", "get").mockReturnValue("zh-CN");
    renderAt("/", fakeApi({ health: vi.fn().mockResolvedValue({ status: "ok", service: "quantos-research-api", api_version: "v1", runtime_mode: "DEMO", deployment_mode: "public_preview", data_label: "SYNTHETIC_FIXTURE", quantos_version: "0.3.1", build_commit: "demo123" }) }));
    expect(await screen.findByRole("heading", { name: "可审计的 AI 金融研究工作台" })).toBeInTheDocument();
    expect(screen.getByText("演示数据", { exact: true })).toBeInTheDocument();
  });

  it("switches languages and persists the selection locally", async () => {
    const first = renderAt();
    await screen.findByRole("heading", { name: /Auditable AI Financial/ });
    await userEvent.selectOptions(screen.getByLabelText("Language"), "zh-CN");
    expect(screen.getByRole("heading", { name: "可审计的 AI 金融研究工作台" })).toBeInTheDocument();
    expect(window.localStorage.getItem("quantos.locale")).toBe("zh-CN");
    first.unmount();
    renderAt();
    expect(await screen.findByRole("heading", { name: "可审计的 AI 金融研究工作台" })).toBeInTheDocument();
  });

  it("explains non-ready capability states without changing backend truth", async () => {
    renderAt();
    const evidence = (await screen.findByText("External evidence")).closest("article");
    expect(evidence).not.toBeNull();
    expect(within(evidence!).getByLabelText("Status UNAVAILABLE")).toBeInTheDocument();
    await userEvent.click(within(evidence!).getByText("Why?"));
    expect(within(evidence!).getByText(/does not connect an external evidence provider/)).toBeInTheDocument();
    expect(screen.getByText("Technical status")).toBeInTheDocument();
  });

  it("populates a fixture-safe example and completes Ask", async () => {
    const { api } = renderAt("/ask");
    const example = "What is the latest market closing price?";
    await userEvent.click(screen.getByRole("button", { name: example }));
    expect(screen.getByLabelText("Research question")).toHaveValue(example);
    await userEvent.click(screen.getByRole("button", { name: "Ask QuantOS" }));
    expect(await screen.findByText(askResult.answer)).toBeInTheDocument();
    expect(api.ask).toHaveBeenCalledWith(expect.objectContaining({ question: example, no_research: true }));
  });

  it("dismisses the first-run guide and preserves dismissal", async () => {
    const first = renderAt();
    expect(screen.getByLabelText("A three-step research flow")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Got it" }));
    expect(screen.queryByLabelText("A three-step research flow")).not.toBeInTheDocument();
    expect(window.localStorage.getItem("quantos.firstRunGuide.dismissed")).toBe("true");
    first.unmount(); renderAt();
    await waitFor(() => expect(screen.queryByLabelText("A three-step research flow")).not.toBeInTheDocument());
  });

  it("keeps feedback local and exposes a copy action without identity fields", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window.navigator, "clipboard", { configurable: true, value: { writeText } });
    renderAt();
    await userEvent.click(screen.getByRole("button", { name: "Feedback" }));
    const dialog = screen.getByRole("dialog", { name: "Preview feedback" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Useful" }));
    await userEvent.type(within(dialog).getByLabelText("Optional feedback"), "Clear audit path");
    await userEvent.click(within(dialog).getByRole("button", { name: "Copy Feedback" }));
    expect(writeText).toHaveBeenCalledOnce();
    const copied = String(writeText.mock.calls[0][0]);
    expect(copied).toContain("Clear audit path");
    expect(copied).not.toMatch(/email|name|user_id/i);
  });
});
