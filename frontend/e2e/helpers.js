import { expect } from "@playwright/test";

// Everything the app asks the backend for during these tests, answered in the
// browser. Nothing here touches a real server, a database or a government
// website - the checks are about what the admin sees, not about ingestion.
export const DEFAULTS = {
  "GET /api/v1/auth/me": { email: "e2e-admin@test.invalid", role: "admin" },
  "GET /api/v1/admin/sources": [
    {
      source_id: "SRC-AU-189",
      country_id: "AU",
      program_id: "SKILLED_189",
      visa_category: "SKILLED",
      authority: "Department of Home Affairs",
      url: "https://immi.homeaffairs.gov.au/visas/189",
      source_type: "HTML",
      priority: "PRIMARY",
      monitoring_method: "WEB",
      crawl_frequency: "DAILY",
      status: "ACTIVE",
      last_checked_at: "2026-10-10T03:00:00Z",
      consecutive_failures: 0,
      blocked_until: null,
    },
  ],
  "GET /api/v1/admin/requirements/review-queue": {
    requirements: [],
    status: "NEEDS_REVIEW",
    program_id: null,
    count: 0,
  },
  "GET /api/v1/admin/rules/queue": { counts: { drafts: 0, validated: 0 } },
  "GET /api/v1/admin/scheduler": {
    enabled: true,
    mode: "daily",
    hour_utc: 3,
    minute_utc: 0,
    interval_minutes: null,
    next_run_at: "2026-10-11T03:00:00Z",
  },
  "GET /api/v1/admin/rules": [],
  "GET /api/v1/admin/rules/gold-examples": { items: [], warning: "" },
  "GET /api/v1/admin/documents": [],
};

// Sign in the way the app does: a token in sessionStorage plus a /me that
// says the bearer is an admin. The token is a placeholder - every request is
// intercepted, and the client never verifies it itself.
export async function signIn(page) {
  await page.addInitScript(() => {
    sessionStorage.setItem("ik_admin_token", "e2e-placeholder-token");
  });
}

// Mock every API call. `overrides` maps "METHOD /path" to a JSON body, a
// function of the request, or {status, body}.
export async function mockApi(page, overrides = {}) {
  const routes = { ...DEFAULTS, ...overrides };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const { pathname } = new URL(request.url());
    const key = `${request.method()} ${pathname}`;
    const handler = routes[key];
    if (handler === undefined) {
      await route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ detail: `No mock for ${key}` }),
      });
      return;
    }
    const value = typeof handler === "function" ? await handler(request) : handler;
    if (value && typeof value === "object" && "status" in value && "body" in value) {
      await route.fulfill({
        status: value.status,
        contentType: "application/json",
        body: JSON.stringify(value.body),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(value),
    });
  });
}

export async function openPage(page, path, overrides) {
  await signIn(page);
  await mockApi(page, overrides);
  await page.goto(path);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
}

export async function openApplicantPage(page, path, overrides = {}) {
  await page.addInitScript(() => {
    sessionStorage.setItem("ik_applicant_token", "e2e-applicant-placeholder-token");
  });
  await mockApi(page, {
    ...DEFAULTS,
    "GET /api/v1/auth/me": {
      user_id: "applicant-user-1",
      email: "applicant@example.test",
      full_name: "Alex Applicant",
      role: "applicant",
    },
    ...overrides,
  });
  await page.goto(path);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
}
