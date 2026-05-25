# WGAI Entry Form Tracker

Public status page:

Set `TRACKER_URL` to the current production deployment URL. The code falls
back to `https://wgai-monitor.vercel.app/`, but the monitor no longer depends
on that hardcoded domain when `TRACKER_URL`, `PUBLIC_TRACKER_URL`,
`VERCEL_PROJECT_PRODUCTION_URL`, or `VERCEL_URL` is available.

This project watches the WGAI membership page for the short text containing
`Entry Form for Amateur Players` and highlights whether the live text has
changed from the baseline.

Current baseline: `Entry Form for Amateur Players - Season 2026 (Leg 7 to 8)`.
Telegram alerts are sent only when this monitored text changes.
If WGAI adds another amateur entry form below the old one, the monitor tracks
the combined list of matching entry-form items so additions and removals are
detected too.

Hosted checks run every 5 minutes through GitHub Actions. The share page on
Vercel is read-only and safe to open publicly. If a GitHub Actions runner
cannot reach `wgai.co.in` directly, the monitor falls back to the deployed
Vercel API before treating the check as a hard failure. For that fallback to
work from GitHub Actions, set the repository variable `TRACKER_URL` to the
live Vercel URL.

If WGAI is temporarily unreachable from Vercel too, the public page shows the
last recorded observation marked as not live. That recorded value is never
used to send a Telegram change alert or to pass the uptime check.
