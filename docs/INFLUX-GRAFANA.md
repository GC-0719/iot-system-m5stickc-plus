# InfluxDB and Grafana, from zero

Everything here is written against *this* project's data, so you can paste any
command straight in. Read part 1 before running the forwarder and part 2 before
opening Grafana.

---

## Part 1 — InfluxDB

### What it is

A database built for one shape of data: **numbers that arrive with a timestamp
and are never edited afterwards.** That is exactly what a 50 Hz accelerometer
produces.

You could store this in MySQL. It would work until it didn't — a `(time, device,
ax, ay, az, gx, gy, gz)` table takes 4.3 million rows per device per day at
50 Hz, needs an index on `time` you have to maintain yourself, has no idea what
"average az per second over the last 5 minutes" means without a slow `GROUP BY`
on a computed column, and will never delete old rows unless you write a cron job.
InfluxDB does all four of those as built-in behavior.

That is also why this project still uses MySQL — for the small, *mutable*,
relational facts (which device is where, its calibration, which benchmark run a
time window belongs to). Different jobs.

### The vocabulary

Four concepts. The SQL column is an analogy, not an equivalence.

| InfluxDB | Roughly like | In our data |
|---|---|---|
| **database** | database | `cps_hw1` |
| **measurement** | table | `imu`, `health`, `pipeline` |
| **tag** | indexed VARCHAR column | `device_id=sim-000001`, `site=apt` |
| **field** | unindexed value column | `ax=0.0014`, `az=1.0044` |
| **time** | mandatory primary key | every point has one, always |

The two rules that follow from this, which is most of what you need to know:

1. **Tags are always strings and are always indexed.** You filter and group by
   them. `WHERE device_id = 'sim-000001'` is fast.
2. **Fields are the actual numbers and are *not* indexed.** You do maths on them.
   `WHERE az > 1.5` works but scans, so it is slow on a big range.

### Series and cardinality — the one thing that kills InfluxDB

A **series** is one unique combination of measurement plus tag values. Our IMU
data is a single series:

```
imu,device_id=sim-000001,site=apt,fw=sim-0.1.0
```

Fifty points a second all land in that one series. InfluxDB holds an index entry
per series in memory, so cost scales with the number of *series*, not the number
of points.

Now imagine we had made `seq` a tag instead of a field. Every message has a new
`seq`, so every message would mint a brand-new series. One per second, 86,400 a
day, each with its own index entry — the database would be unusable within
hours. This failure has a name, **cardinality explosion**, and it is the single
most common way people wreck an InfluxDB.

> **The rule:** a tag must be something with a small, bounded set of possible
> values. Device IDs, site names, firmware versions — yes. Timestamps, sequence
> numbers, measured values, UUIDs — never.

### Line protocol

The text format for writing data. One line is one point:

```
imu,device_id=sim-000001,fw=sim-0.1.0,site=apt ax=0.00145,az=1.00439 1788939733857700096
└─┬─┘ └──────────────── tags ────────────────┘ └───── fields ─────┘ └──── time (ns) ────┘
 measurement
```

Note the separators: a **comma** between measurement and tags and between tags,
a **space** before the fields, another **space** before the timestamp. Getting
those wrong is the usual cause of a rejected write. `forwarder.py` builds these
strings in its `line()` function — read it, it's fifteen lines.

The timestamp is **nanoseconds since the Unix epoch** by default. Ours are
derived, not sent: the device sends `start_ts` and `freq`, and the forwarder
computes sample *i* as `start_ts + i/freq`.

### Retention policies

A retention policy is an automatic expiry rule. Every point lives inside exactly
one, and InfluxDB deletes points once they age past its `DURATION`. We have two:

| Policy | Holds | Duration | Shard |
|---|---|---|---|
| `rp_7d` | raw 50 Hz waveform | 7 days | 1 hour |
| `rp_90d` | 1 Hz downsampled means | 90 days | 1 day |

A **continuous query** runs on a schedule and populates `rp_90d` from `rp_7d`.
So a dashboard showing "last 10 minutes" reads raw data, and one showing "last
month" reads the cheap summary. Shard duration is the chunk size InfluxDB drops
when expiring — small shards on the hot policy make deletion cheap.

### InfluxQL — the query language

It looks like SQL and mostly behaves like it. Open the shell:

```bash
influx -database cps_hw1
```

Start by looking around:

```sql
SHOW MEASUREMENTS
SHOW TAG KEYS FROM imu
SHOW FIELD KEYS FROM imu
SHOW SERIES FROM imu              -- how many series exist. Watch this number.
SELECT * FROM imu ORDER BY time DESC LIMIT 5
```

Then the query that matters:

```sql
SELECT mean("az") FROM "imu"
WHERE time > now() - 5m
GROUP BY time(1s), "device_id"
```

**`GROUP BY time(1s)` is the whole point of InfluxDB.** It buckets by time
interval — one row per second, each the mean of the 50 raw samples inside it.
Without bucketing, a five-minute range returns 15,000 rows and your browser dies
trying to plot them.

Two InfluxQL gotchas that will catch you:

- If you `GROUP BY time()`, every selected field **must** have an aggregate
  function on it. `SELECT az ... GROUP BY time(1s)` is an error; `SELECT
  mean(az)` is fine.
- Double quotes are for identifiers, single quotes for string values.
  `WHERE "device_id" = 'sim-000001'`. Backwards, and it silently matches nothing.

Useful ones for this project:

```sql
-- Is data arriving at all?
SELECT count("az") FROM "imu" WHERE time > now() - 1m

-- Signal magnitude: should sit near 1.0 g at rest, spike when "shaking"
SELECT mean("ax")*mean("ax") + mean("ay")*mean("ay") + mean("az")*mean("az")
FROM "imu" WHERE time > now() - 5m GROUP BY time(1s)

-- How stale is the data by the time it lands?
SELECT mean("e2e_latency_ms"), max("e2e_latency_ms")
FROM "pipeline" WHERE time > now() - 15m GROUP BY time(10s)

-- Have we dropped anything?
SELECT sum("gap_count") FROM "pipeline" WHERE time > now() - 1h
```

---

## Part 2 — Grafana

### What it is

Grafana stores no data. It is a web page that runs queries against InfluxDB on a
timer and draws the answers. If a panel is empty, the problem is almost always
the query or the time range, not Grafana.

Four nested things:

- **Data source** — a connection to InfluxDB (URL + database). Configure once.
  `04_grafana.sh` already provisioned ours as `InfluxDB-cps`.
- **Dashboard** — one page.
- **Panel** — one chart on that page.
- **Query** — each panel owns one or more. This is where InfluxQL goes.

### The thing that confuses everyone first

**Your query does not contain a time range.** The time picker in the top-right
corner of the dashboard supplies it, injecting a `WHERE time > ... AND time <
...` clause into every panel automatically.

So a panel showing nothing usually means the time picker is pointing somewhere
your data isn't. Set it to **Last 15 minutes** with a **5s** refresh and leave it
there while you develop.

This is also where bad clocks become visible. If the sensor's clock is ten
minutes fast, its points are written ten minutes *in the future* — and "Last 15
minutes" will not show them, because as far as Grafana is concerned they haven't
happened yet. **That is the concrete reason every clock in this system is
synced to one source**, and why the `pipeline` measurement tracks skew.

### Building your first panel

Do this one by hand in the UI rather than importing a JSON file — clicking
through the query editor once is how the model actually lands.

1. Open `http://<vm-ip>:3000`, log in with `admin` / `admin`.
2. **+ → Dashboard → Add new panel.**
3. The query editor at the bottom has a row of dropdowns. Set:
   - `FROM` → default → `imu`
   - `WHERE` → `device_id` = `sim-000001`
   - `SELECT` → field(`az`) → mean()
   - `GROUP BY` → time(`$__interval`) → fill(`null`)
4. Click the pencil icon on the query row. It shows you the InfluxQL it just
   built. It's the query from part 1 — the dropdowns are a typing aid, nothing
   more.
5. Set the time range to **Last 15 minutes**, refresh to **5s**.

`$__interval` is a Grafana variable holding "one pixel's worth of time" for the
current panel width and range. It's why the panel stays fast whether you're
looking at 5 minutes or 5 days.

### Three core panels

**IMU waveform.** `FROM imu`, `SELECT mean(ax)`, `mean(ay)`, `mean(az)` as three
separate queries, `GROUP BY time($__interval)`. You should see `az` sitting at
1.0 (gravity) and all three go lively when the simulator hits its `walking` and
`shaking` states.

**Pipeline health.** `FROM pipeline`, `SELECT mean(e2e_latency_ms)`. This shows
clock-sync and transport health in one glance.

**Device status.** A Stat panel, `FROM health`, `SELECT last(vbat)`. Add
`last(rssi)` next to it.

Once they look right, **export the dashboard JSON** (dashboard settings → JSON
Model) into `server/grafana/dashboards/` so it is reproducible and in the repo.

---

## When a panel says "No data"

Work down this list. It is almost always #1 or #2.

1. **Time range.** Set it to Last 15 minutes. Then try Last 24 hours — if data
   appears there but not in the recent window, your clocks disagree — check
   that the sensor is syncing to the server.
2. **Is anything arriving?** `SELECT count(*) FROM imu WHERE time > now() - 5m`
   in the influx shell. Zero means the problem is upstream of Grafana entirely.
3. **Is the forwarder running and connected?** It prints `points written: N`
   every ten seconds. If N never moves, check it can reach the broker.
4. **Is the broker receiving?** `mosquitto_sub -h <vm-ip> -t 'cps/hw1/#' -v`.
   If nothing prints, the simulator isn't publishing or can't connect.
5. **Quoting.** `WHERE "device_id" = 'sim-000001'` — double quotes on the
   identifier, single on the value.
6. **Aggregate function.** Any `GROUP BY time()` requires `mean()`, `last()`,
   etc. on every selected field.
