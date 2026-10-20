# AI processing framework (HW2, requirement 2)

Runs the **same analyses** over sensor data whether it arrives live from MQTT or is read
back from InfluxDB. What to process is set entirely by [`config.yaml`](config.yaml) — no
measurement, field, or analysis is named in code.

```
                         ┌──────────────────┐
   MQTT (live)  ───────▶ │ main_ai_mqtt.py  │ ─┐
                         └──────────────────┘  │   ┌────────────────┐
                              both import       ├─▶ │  processing.py │  (pure functions:
                         ┌──────────────────┐  │   └────────────────┘   sma, rms, variance,
   InfluxDB (archive)─▶  │ main_ai_influx.py│ ─┘                        dominant_freq, activity)
                         └──────────────────┘
                              │                         results written back as ai_<measurement>
                              └──────────────▶ InfluxDB (ai_imu, ai_audio) ──▶ Grafana
```

## Files

| File | Role |
|---|---|
| `config.yaml` | The only place measurements/fields/analyses are named. Read at runtime. |
| `processing.py` | Pure analysis functions over one window. No I/O. Shared by both entry points. |
| `main_ai_mqtt.py` | Live: subscribe, keep a rolling window per device, analyse, publish to `cps/hw1/<dev>/ai` + write `ai_*`. |
| `main_ai_influx.py` | Retrospective: analyse a `--start_time`/`--end_time` window from InfluxDB, write `ai_*`. |
| `build.sh` | Nuitka build → standalone binaries (requirement 3). |

## Run

```bash
# live processing of the real-time MQTT stream
python3 main_ai_mqtt.py                         # uses ./config.yaml

# retrospective processing of archived data (required --start_time/--end_time)
python3 main_ai_influx.py --start_time "now()-30m" --end_time "now()"
python3 main_ai_influx.py --start_time 2026-10-19T00:00:00Z \
                          --end_time   2026-10-19T01:00:00Z --dry-run
```

Both accept `--config <path>` to use a different YAML.

## No hard coding — the point of the YAML

The default config runs `sma, rms, dominant_freq, activity` over `ax, ay, az` and
`rolling_mean, variance` over `rms_db`. Point either entry point at a different YAML and the
processing changes with **no code edit** — e.g. a config naming only `imu/az` with `variance`
produces only `var_az`, and nothing else. After the Nuitka build (requirement 3) this still
holds: the binary reads `config.yaml` at runtime.

## Analyses (`processing.py`)

| Name | Output fields | Meaning |
|---|---|---|
| `sma` | `sma` | Signal-magnitude area: mean of Σ\|field\| over the window |
| `rms` | `rms_<field>`, `rms_mag` | Per-field and combined-magnitude RMS |
| `variance` | `var_<field>` | Per-field variance |
| `rolling_mean` | `mean_<field>` | Per-field mean |
| `dominant_freq` | `dominant_freq` | FFT peak frequency (Hz) of the magnitude signal |
| `activity` | `activity_code` (0/1/2), `activity_intensity`, `activity_freq` | Still / walking / vigorous, gated on **AC energy** (motion after gravity is removed), not raw magnitude |

Add an analysis by adding one function and one `REGISTRY` entry; the entry points pick it up
by name from the config.

## Validated

- `main_ai_influx.py` over real archived data: 194 windows → `ai_imu` + `ai_audio`.
- `main_ai_mqtt.py` live: tracked the simulator's still→walking→shaking cycle as
  `activity_code` 0→1→2, processing two devices and two measurements concurrently; results
  written with `src=mqtt` (vs `src=influx` for the retrospective path).

## Reliability (per the HW2 notes)

The notes require rigor about unreliable links. The live path (`main_ai_mqtt.py`) subscribes with
QoS 1 on a persistent session (missed messages are redelivered after a restart), buffers samples by
their **own timestamp** so out-of-order or delayed batches are placed correctly rather than appended,
tolerates lost packets (each window is computed over whatever samples are present), and counts gaps
from the sequence number. The archived path (`main_ai_influx.py`) reads time-ordered data and retries
transient query failures. Verified with an out-of-order + dropped-batch test: windows emit in
timestamp order, the lost batch is counted, and the analysis is unaffected.
