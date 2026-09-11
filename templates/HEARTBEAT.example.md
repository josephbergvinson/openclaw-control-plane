# HEARTBEAT.md — Periodic attention checks

Use this file only when the installed runtime is configured to run a heartbeat that reads it. Copying this file does not create a schedule. It is non-authoritative and does not replace `AGENTS.md`, the native scheduler, a running task or a provider's current state.

Read the configured attention sources using the permitted bounded read paths. Report a material change, failure, decision or requested reminder in natural prose. A no-change success stays silent unless the schedule explicitly promises a report. Do not repeat unchanged alerts or treat stale evidence as proof of failure.

If a task remains active, its own supported task/session primitive owns continuation and delivery. Do not create a second worker, replay a possible effect or infer liveness from an old status file. Use the existing native status and reconciliation paths. Any repair must remain inside an authenticated instruction or explicitly configured standing grant; this file creates neither.
