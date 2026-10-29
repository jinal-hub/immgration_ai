import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import SourcesPage from "../SourcesPage.jsx";
import { ToastProvider } from "../../components/ToastContext.jsx";
import * as api from "../../api/client.js";

vi.mock("../../api/client.js");

// Far enough in the future that the test does not race the clock, and far
// enough in the past that "expired" is unambiguous.
const FUTURE = new Date(Date.now() + 60 * 60 * 1000).toISOString();
const PAST = new Date(Date.now() - 60 * 60 * 1000).toISOString();

function source(overrides = {}) {
  return {
    source_id: "SRC-AU-189",
    country_id: "AU",
    program_id: "SKILLED_189",
    visa_category: "SKILLED",
    authority: "Home Affairs",
    url: "https://immi.homeaffairs.gov.au/189",
    source_type: "HTML",
    priority: "PRIMARY",
    monitoring_method: "WEB",
    crawl_frequency: "DAILY",
    status: "ACTIVE",
    last_checked_at: null,
    last_successful_fetch_at: null,
    last_hash: null,
    notes: null,
    fallback_source_id: null,
    fetch_status: null,
    http_status: null,
    last_error: null,
    last_error_type: null,
    consecutive_failures: 0,
    created_at: null,
    blocked_until: null,
    ...overrides,
  };
}

function renderPage() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <SourcesPage />
      </ToastProvider>
    </MemoryRouter>
  );
}

async function renderWith(sources) {
  api.listSources.mockResolvedValue(sources);
  renderPage();
  await waitFor(() =>
    expect(screen.getByText(sources[0].source_id)).toBeInTheDocument()
  );
}

describe("Sources table shows a host's refusal cooldown", () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it("disables Run now and labels it Paused while the host is refused", async () => {
    await renderWith([
      source({ source_id: "SRC-PAUSED", blocked_until: FUTURE }),
      source({ source_id: "SRC-OK", url: "https://other.example.invalid/1" }),
    ]);

    const paused = screen.getAllByRole("button", { name: "Paused" });
    expect(paused).toHaveLength(1);
    expect(paused[0]).toBeDisabled();
    expect(paused[0]).toHaveAttribute(
      "title",
      expect.stringContaining("refused our last request")
    );

    const runnable = screen.getAllByRole("button", { name: "Run now" });
    expect(runnable).toHaveLength(1);
    expect(runnable[0]).toBeEnabled();
  });

  it("publishes the moment the host will be reachable again", async () => {
    await renderWith([
      source({ source_id: "SRC-PAUSED", blocked_until: FUTURE }),
      source({ source_id: "SRC-OK", url: "https://other.example.invalid/1" }),
    ]);

    const paused = screen.getByRole("button", { name: "Paused" });
    const title = paused.getAttribute("title");
    expect(title).toMatch(/Sync resumes after/);
    expect(title).toContain(new Date(FUTURE).toLocaleString());

    // The reachable source shows no resume time at all.
    expect(screen.getByRole("button", { name: "Run now" }).getAttribute("title"))
      .toBeNull();
  });

  it("treats an already-expired pause as reachable", async () => {
    await renderWith([
      source({ source_id: "SRC-EXPIRED", blocked_until: PAST }),
    ]);

    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Run now" })).toBeEnabled();
  });

  it("leaves Run now alone when the host has never refused anything", async () => {
    await renderWith([source()]);

    expect(screen.getByRole("button", { name: "Run now" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Run now" }).getAttribute("title"))
      .toBeNull();
  });

  it("shows the fetch result, HTTP status, and browser error in the source list", async () => {
    await renderWith([
      source({
        last_checked_at: "2026-10-10T03:00:00Z",
        fetch_status: "ACCESS_BLOCKED",
        http_status: 403,
        last_error: "Playwright navigation was refused",
        last_error_type: "http_error",
      }),
    ]);

    expect(screen.getByText("Blocked")).toBeInTheDocument();
    expect(screen.getByText("HTTP 403")).toBeInTheDocument();
    expect(screen.getByText("Playwright navigation was refused")).toBeInTheDocument();
    expect(screen.getByText(new Date("2026-10-10T03:00:00Z").toLocaleString()))
      .toBeInTheDocument();
  });

  it("explains blank diagnostics when a never-fetched source is paused", async () => {
    await renderWith([
      source({ blocked_until: FUTURE }),
    ]);

    expect(screen.getByText("Not checked (host paused)")).toBeInTheDocument();
    expect(screen.getByText("Skipped (host paused)")).toBeInTheDocument();
  });
});

describe("409 responses tell busy and refused apart", () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it("shows the host's own explanation for a cooldown 409", async () => {
    const user = userEvent.setup();
    api.syncSource.mockRejectedValue({
      status: 409,
      body: {
        detail: {
          code: "host_cooldown",
          host: "immi.homeaffairs.gov.au",
          blocked_until: FUTURE,
          message:
            "Host immi.homeaffairs.gov.au refused our last request and is paused until 2026-10-09T00:00:00+00:00.",
        },
      },
    });
    await renderWith([source()]);

    await user.click(screen.getByRole("button", { name: "Run now" }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        /refused our last request/i
      )
    );
    expect(screen.getByRole("status")).not.toHaveTextContent(
      /already in progress/i
    );
  });

  it("keeps the ordinary busy message for a lock 409", async () => {
    const user = userEvent.setup();
    api.syncSource.mockRejectedValue({
      status: 409,
      body: { detail: "A sync is already in progress" },
    });
    await renderWith([source()]);

    await user.click(screen.getByRole("button", { name: "Run now" }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        /already in progress/i
      )
    );
  });
});
