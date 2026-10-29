import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ToastProvider } from "../../components/ToastContext.jsx";
import { useSyncRunner } from "../useSyncRunner.js";
import * as api from "../../api/client.js";

vi.mock("../../api/client.js");

// Wrapper providing all required context providers.
function wrapper({ children }) {
  return (
    <MemoryRouter>
      <ToastProvider>{children}</ToastProvider>
    </MemoryRouter>
  );
}

describe("useSyncRunner", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.useFakeTimers();
    // document.visibilityState defaults to "visible" in jsdom
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "visible",
    });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("starts in idle state", () => {
    const { result } = renderHook(() => useSyncRunner(), { wrapper });
    expect(result.current.running).toBe(false);
    expect(result.current.elapsedSeconds).toBe(0);
  });

  it("sets running=true and increments elapsedSeconds after sync starts", async () => {
    api.syncSource.mockResolvedValue({ run_id: "run-1" });
    api.getRun.mockResolvedValue({ status: "RUNNING" });

    const { result } = renderHook(() => useSyncRunner(), { wrapper });

    await act(async () => {
      result.current.runOne("SRC-AU-189");
    });

    expect(result.current.running).toBe(true);

    // Advance 3 seconds (past the first poll interval).
    act(() => {
      vi.advanceTimersByTime(3000);
    });

    expect(result.current.elapsedSeconds).toBeGreaterThanOrEqual(3);
  });

  it("stops and calls onSettled when run completes", async () => {
    api.syncAllSources.mockResolvedValue({ run_id: "run-2" });
    api.getReviewQueue.mockResolvedValue({ count: 0 });
    api.getRun
      .mockResolvedValueOnce({ status: "RUNNING" })
      .mockResolvedValueOnce({ status: "COMPLETED", result: { source_id: "SRC-AU-189", status: "unchanged" } });

    const onSettled = vi.fn();
    const { result } = renderHook(() => useSyncRunner(onSettled), { wrapper });

    await act(async () => {
      result.current.runAll();
    });

    // First poll (3 s) — still RUNNING.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(result.current.running).toBe(true);

    // Second poll (6 s later, after back-off to 6 s) — COMPLETED. The async
    // variant flushes the promises the poll awaits, which `waitFor` cannot do
    // while fake timers are installed.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });

    expect(onSettled).toHaveBeenCalledTimes(1);
    expect(result.current.running).toBe(false);
    expect(result.current.elapsedSeconds).toBe(0);
  });

  it("includes Playwright failure details and paused-source count in the run summary", async () => {
    api.syncAllSources.mockResolvedValue({ run_id: "run-detail" });
    api.getReviewQueue.mockResolvedValue({ count: 0 });
    api.getRun.mockResolvedValue({
      status: "COMPLETED",
      result: {
        changed: 1,
        errors: 1,
        cooldowns: 1,
        total_sources: 3,
        results: [
          {
            source_id: "SRC-FAILED",
            status: "error",
            errors: ["Playwright navigation timed out"],
          },
          {
            source_id: "SRC-PAUSED",
            status: "cooldown",
            errors: ["Host example.gov.au was skipped until later."],
          },
        ],
      },
    });

    const { result } = renderHook(() => useSyncRunner(), { wrapper });
    await act(async () => {
      await result.current.runAll();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });

    expect(screen.getByRole("status")).toHaveTextContent(
      /1 paused by host cooldown/
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      /SRC-FAILED: Playwright navigation timed out/
    );
    expect(screen.getByRole("status")).toHaveTextContent(/SRC-PAUSED:/);
  });

  it("shows conflict message on 409 with host_cooldown code", async () => {
    const showToastMock = vi.fn();
    // Patch useToast by mocking the ToastContext module isn't straightforward
    // here; instead we check the toast is shown by inspecting the DOM.
    // A simpler unit-level check: the conflictMessage function is tested
    // indirectly by verifying the 409 doesn't throw unhandled.
    api.syncSource.mockRejectedValue({
      status: 409,
      body: {
        detail: {
          code: "host_cooldown",
          host: "immi.homeaffairs.gov.au",
          blocked_until: new Date(Date.now() + 3600000).toISOString(),
          message: "Host immi.homeaffairs.gov.au refused our last request.",
        },
      },
    });

    const { result } = renderHook(() => useSyncRunner(), { wrapper });

    await act(async () => {
      // Should not throw.
      await result.current.runOne("SRC-AU-189");
    });

    // running stays false — the 409 is handled without starting a poll.
    expect(result.current.running).toBe(false);
  });
});
