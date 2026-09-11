# QuantOS scheduler systemd templates

These repository templates wake the scheduler every five minutes. Each service
invocation evaluates all application-defined slots once and exits. Application
policy, missed-run recovery, claims, retry and fencing remain inside QuantOS.

Before manual installation, replace `/opt/quantos` and `/var/lib/quantos` with
the deployment paths. Keep the calendar artifact updated through a separately
controlled offline process; an uncovered date fails closed instead of being
treated as a market holiday. The supplied service uses explicit `strict_live`
mode and `--no-llm`.

Installation and activation are intentionally not automated by this project.
An operator may copy the reviewed units to the system unit directory, reload
systemd, and enable the timer using the host's normal administrative process.
Do not place provider credentials or authorization values in these unit files.

The adapter has no heartbeat loop. Configure its maximum execution duration to
remain below the scheduler-runtime lease. A process killed after external work
but before finalization may be retried after lease expiry; exactly-once business
execution is not guaranteed.
