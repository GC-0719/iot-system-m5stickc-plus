-- Device registry and run labels.
--
-- The split of duties matters: InfluxDB holds the append-only, time-indexed
-- waveform; MySQL holds the small, MUTABLE, relational facts about it. A
-- calibration offset gets corrected. A device gets redeployed. A benchmark run
-- gets named after the fact. None of those belong in a time-series database.

CREATE DATABASE IF NOT EXISTS cps_hw1
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE cps_hw1;

-- One row per physical sensor. device_id matches the InfluxDB tag exactly, so
-- this table joins to the time series on a value you can read off the board.
CREATE TABLE IF NOT EXISTS device (
  device_id     VARCHAR(32)  NOT NULL,
  hardware      VARCHAR(64)  NOT NULL,           -- 'M5StickC PLUS (K016-P)'
  mac           CHAR(17)             DEFAULT NULL,
  site          VARCHAR(32)  NOT NULL DEFAULT 'apt',
  fw_version    VARCHAR(32)          DEFAULT NULL,
  sample_hz     SMALLINT UNSIGNED    DEFAULT 50,
  commissioned  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  decommissioned DATETIME            DEFAULT NULL,
  notes         VARCHAR(255)         DEFAULT NULL,
  PRIMARY KEY (device_id),
  KEY idx_site (site)
) ENGINE=InnoDB;

-- IMU zero-offset and scale, per axis. Applied at analysis time rather than
-- baked into the stored samples, so raw data stays raw and a re-calibration
-- does not require rewriting history.
CREATE TABLE IF NOT EXISTS calibration (
  device_id   VARCHAR(32) NOT NULL,
  axis        ENUM('ax','ay','az','gx','gy','gz') NOT NULL,
  offset_val  DOUBLE      NOT NULL DEFAULT 0,
  scale_val   DOUBLE      NOT NULL DEFAULT 1,
  measured_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (device_id, axis),
  CONSTRAINT fk_cal_device FOREIGN KEY (device_id)
    REFERENCES device (device_id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- Benchmark runs (ARCHITECTURE.md §7). Each row names a time window and the
-- configuration in force during it, so a Grafana annotation can say which
-- factor level produced which CPU and RAM curve.
CREATE TABLE IF NOT EXISTS bench_run (
  run_id      INT UNSIGNED NOT NULL AUTO_INCREMENT,
  label       VARCHAR(64)  NOT NULL,             -- 'C: batch=500'
  factor      CHAR(1)      NOT NULL,             -- A..G
  settings    JSON                 DEFAULT NULL,
  started_at  DATETIME     NOT NULL,
  ended_at    DATETIME             DEFAULT NULL,
  notes       VARCHAR(255)         DEFAULT NULL,
  PRIMARY KEY (run_id),
  KEY idx_factor_time (factor, started_at)
) ENGINE=InnoDB;

-- Seed the two devices this project uses.
INSERT INTO device (device_id, hardware, site, fw_version, sample_hz, notes)
VALUES
  ('sim-000001', 'sensor_sim.py',           'apt', 'sim-0.1.0', 50,
   'Synthetic source used to build the pipeline before hardware'),
  ('m5-000000',  'M5StickC PLUS (K016-P)',  'apt', 'hw1-0.1.0', 50,
   'Placeholder - replace device_id with the real MAC suffix once flashed')
ON DUPLICATE KEY UPDATE hardware = VALUES(hardware);
