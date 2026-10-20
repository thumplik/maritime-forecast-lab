# Cutoff sampling

Normative source: build plan §6.3.

| Parameter | Default | Meaning |
|---|---|---|
| `underway_min_sog_kn` | 2.0 | mean SOG over the 10 min ending at the cutoff must be ≥ this for the underway set |
| `min_history_reports` | 6 | minimum reports in the history window |
| `history_hours` | 3 | history window for the report-count check (audit); models may use more |
| `min_spacing_hours` | 3 | minimum time between cutoffs on the same vessel |
| `max_per_vessel_day` | 2 | cap on cutoffs per vessel per UTC day |
| `region` | `houston_galveston` | cutoff must lie in this region |
| `seed` | 20230601 | RNG seed for the random choice among eligible observations |

Algorithm (per vessel, per UTC day): list eligible observations in time order, shuffle with the seeded RNG, accept greedily while respecting spacing and the daily cap. Stationary set: same procedure with mean SOG < 0.5 kn; never pooled with underway in headline metrics.

Sampler config and seed are written to every run's sidecar.
