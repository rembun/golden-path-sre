# Evidence: retry-storm run, 24 November 2026

Captured from the local cluster during `make load SCENARIO=storm` (k3s, 3 to 5 API pods). All
times UTC. The 30-day panels are dominated by earlier chaos runs on this lab cluster, which is why
"Error budget left" is far below zero (see README, "Start the SLO from clean").

| Screenshot | Time | What it shows |
|---|---|---|
| [Before the storm](grafana-picture/Screenshot%202026-11-24%20122313.png) | 12:23:13 | 1-hour burn rate 3.6×, 2 alerts firing (the stack's always-on Watchdog alerts) |
| [Storm starts](grafana-picture/Screenshot%202026-11-24%20122346.png) | 12:23:46 | Within half a minute: 1-hour burn rate 277×, 6 alerts firing, including the fast-burn page |
| [Storm at its worst](grafana-picture/Screenshot%202026-11-24%20122916.png) | 12:29:16 | 1-hour burn rate 594×, 9 alerts firing; 503s at ~100 req/s while clients retry; p99 latency peaks just above 300 ms |
| [Dependencies and pods](grafana-picture/Screenshot%202026-11-24%20122847.png) | 12:28:47 | Capacity calls switch to `circuit_open` (the breaker refusing calls instead of hammering venue-capacity); breakers open per pod, shown by pod name; the HPA adds two pods during the storm |
| [Alertmanager overview](grafana-picture/Screenshot%202026-11-24%20123110.png) | 12:31:10 | Active alerts rise from 2 to ~11 during the storm |
| [Screenshot 140641](grafana-picture/Screenshot%202026-11-24%20140641.png) | 14:06:41 | TODO (author): describe |
| [Screenshot 140718](grafana-picture/Screenshot%202026-11-24%20140718.png) | 14:07:18 | TODO (author): describe |

[k6-storm-summary.txt](k6-storm-summary.txt): client-side summary of a storm run. 51% of requests
met the SLI (not 5xx and under 300 ms), so the k6 threshold failed, as expected during an
incident. Retries added 7,581 requests on top of 15,122 customer attempts. Response times stayed
low (p99 31 ms) because the breaker answers fast.

**Still worth capturing:** the Alertmanager alert list (or Grafana → Alerting) showing
`AvailabilityApiSLOBurnRate5M` by name while firing. The overview above shows the count, not which
alert fired.
