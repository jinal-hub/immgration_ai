import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import * as api from "../api/client.js";
import { useToast } from "../components/ToastContext.jsx";

// Start at 3 s; double after each poll up to a 10 s ceiling.
const POLL_MIN_MS = 3000;
const POLL_MAX_MS = 10000;

// A 409 means one of two very different things: the sync lock is held, or the
// host behind this source refused a request and is paused (see
// FETCH_COOLDOWN_HOURS). The API tags the second with code "host_cooldown" so
// the two never collapse into the same misleading sentence — a busy server and
// a hostile one deserve different words.
function conflictMessage(err) {
  const detail = err?.body?.detail;
  if (detail && typeof detail === "object" && detail.code === "host_cooldown") {
    return detail.message;
  }
  return "A sync is already in progress — try again shortly.";
}

// Shared "Run now" / "Run all sources" behaviour: polls GET /admin/runs/{id}
// until it settles, then shows a plain-language toast summary linking to
// Review. Pipeline-run internals (run ids, raw statuses) are never shown —
// only a non-blocking "sync in progress" state and the final summary.
//
// Returns { running, elapsedSeconds, runOne, runAll } where:
//   running        — true while a sync job is in flight
//   elapsedSeconds — wall-clock seconds since the sync started (0 when idle)
export function useSyncRunner(onSettled) {
  const [running, setRunning] = useState(false);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const { showToast } = useToast();
  const navigate = useNavigate();

  // Refs so callbacks can always read/mutate the latest value without
  // becoming stale closures.
  const pollTimerRef = useRef(null);
  const elapsedTimerRef = useRef(null);
  const pollIntervalRef = useRef(POLL_MIN_MS);
  const startedAtRef = useRef(null);

  // Always up-to-date copy of onSettled so we don't have to add it to every
  // dependency array below.
  const onSettledRef = useRef(onSettled);
  onSettledRef.current = onSettled;

  // Stop all timers and reset running state.
  const stop = useCallback(() => {
    clearTimeout(pollTimerRef.current);
    clearInterval(elapsedTimerRef.current);
    pollTimerRef.current = null;
    elapsedTimerRef.current = null;
    pollIntervalRef.current = POLL_MIN_MS;
    startedAtRef.current = null;
    setRunning(false);
    setElapsedSeconds(0);
  }, []);

  // Clean up on unmount so a navigated-away component doesn't leak timers.
  useEffect(() => () => stop(), [stop]);

  const summarize = useCallback(
    async (job) => {
      if (job.status === "FAILED") {
        showToast(`Sync failed: ${job.error || "unknown error"}`);
        return;
      }
      const result = job.result || {};
      let reviewCount = null;
      try {
        const queue = await api.getReviewQueue({ status: "NEEDS_REVIEW" });
        reviewCount = queue.count;
      } catch {
        /* best-effort only — don't let this block the summary toast */
      }

      let message;
      if (typeof result.total_sources === "number") {
        const cooldowns = result.cooldowns || 0;
        message = `${result.changed} source${result.changed === 1 ? "" : "s"} updated, ${result.errors} failed`;
        if (cooldowns) {
          message += `, ${cooldowns} paused by host cooldown`;
        }
        const failures = (result.results || []).filter(
          (item) => item.status === "error" || item.status === "cooldown"
        );
        if (failures.length) {
          const details = failures.slice(0, 2).map((item) => {
            const reason =
              item.errors?.[0] || item.last_error || item.fetch_status || item.status;
            const clipped = reason.length > 140 ? `${reason.slice(0, 137)}...` : reason;
            return `${item.source_id}: ${clipped}`;
          });
          message += ` — ${details.join("; ")}`;
          if (failures.length > details.length) {
            message += `; and ${failures.length - details.length} more (see Sources)`;
          }
        }
      } else if (result.source_id) {
        const plain = {
          changed: "content changed",
          unchanged: "unchanged",
          error: "failed",
          no_document: "no document found",
          cooldown: "paused — this host refused a request",
        };
        const reason = result.errors?.[0] || result.last_error;
        message = `${result.source_id}: ${plain[result.status] || result.status}`;
        if (reason) {
          const clipped = reason.length > 180 ? `${reason.slice(0, 177)}...` : reason;
          message += ` — ${clipped}`;
        }
      } else {
        message = "Sync completed";
      }
      if (reviewCount !== null) {
        message += ` — ${reviewCount} requirement${reviewCount === 1 ? "" : "s"} need review`;
      }
      showToast(message, {
        duration: 12000,
        action: { label: "Open Review", onClick: () => navigate("/review") },
      });
    },
    [showToast, navigate]
  );

  // Schedule the next poll. Uses setTimeout so we can adjust the interval
  // dynamically and pause cleanly when the tab is hidden.
  const schedulePoll = useCallback(
    (runId) => {
      clearTimeout(pollTimerRef.current);

      const delay =
        document.visibilityState === "hidden"
          ? POLL_MAX_MS // slow down while tab is in background
          : pollIntervalRef.current;

      pollTimerRef.current = setTimeout(async () => {
        // Silently skip the fetch if the tab went hidden between scheduling
        // and firing — just reschedule at the longer interval.
        if (document.visibilityState === "hidden") {
          schedulePoll(runId);
          return;
        }
        try {
          const job = await api.getRun(runId);
          if (job.status === "COMPLETED" || job.status === "FAILED") {
            stop();
            await summarize(job);
            // Small delay so the DB write for last_checked_at commits before
            // we re-fetch the sources list — the in-memory job registry flips
            // to COMPLETED slightly before SQLAlchemy flushes the final row.
            setTimeout(() => {
              if (onSettledRef.current) onSettledRef.current();
            }, 500);
          } else {
            // Exponential back-off: double the interval up to the ceiling.
            pollIntervalRef.current = Math.min(
              pollIntervalRef.current * 2,
              POLL_MAX_MS
            );
            schedulePoll(runId);
          }
        } catch {
          stop();
        }
      }, delay);
    },
    [stop, summarize]
  );

  // Re-schedule the next poll whenever the tab becomes visible again, so
  // we pick it up quickly instead of waiting out the hidden-tab delay.
  useEffect(() => {
    function onVisibilityChange() {
      if (document.visibilityState === "visible" && pollTimerRef.current !== null) {
        // A poll is scheduled — cancel the slow hidden-tab timer and immediately
        // fire a fresh one at the current (fast) interval.  We need access to
        // the run_id, but that's opaque inside schedulePoll's closure, so we
        // just reset the interval and let the next poll fire quickly.
        clearTimeout(pollTimerRef.current);
        pollTimerRef.current = null;
        // We store the runId on the ref below inside `start`, so we can re-use it.
        if (currentRunIdRef.current) schedulePoll(currentRunIdRef.current);
      }
    }
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, [schedulePoll]);

  // Hold the current run_id so the visibility-change handler can re-use it.
  const currentRunIdRef = useRef(null);

  const start = useCallback(
    (runId) => {
      currentRunIdRef.current = runId;
      pollIntervalRef.current = POLL_MIN_MS;
      startedAtRef.current = Date.now();
      setRunning(true);
      setElapsedSeconds(0);

      // Tick elapsed time every second.
      elapsedTimerRef.current = setInterval(() => {
        if (startedAtRef.current !== null) {
          setElapsedSeconds(
            Math.floor((Date.now() - startedAtRef.current) / 1000)
          );
        }
      }, 1000);

      schedulePoll(runId);
    },
    [schedulePoll]
  );

  const runOne = useCallback(
    async (sourceId) => {
      try {
        const result = await api.syncSource(sourceId);
        start(result.run_id);
      } catch (err) {
        if (err.status === 409) {
          showToast(conflictMessage(err));
        } else {
          showToast(err.message || "Could not start sync");
        }
      }
    },
    [start, showToast]
  );

  const runAll = useCallback(async () => {
    try {
      const result = await api.syncAllSources();
      start(result.run_id);
    } catch (err) {
      if (err.status === 409) {
        showToast(conflictMessage(err));
      } else {
        showToast(err.message || "Could not start sync");
      }
    }
  }, [start, showToast]);

  return { running, elapsedSeconds, runOne, runAll };
}
