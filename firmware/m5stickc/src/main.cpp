// M5StickC PLUS sensor data acquisition firmware.
//
// Two sensors, one cadence:
//   - MPU6886 6-axis IMU, sampled at 50 Hz
//   - SPM1423 PDM microphone, sampled at 16 kHz and reduced ON THE DEVICE to a
//     loudness figure per 20 ms block, which lands at the same 50 Hz
//
// Both are batched one second per message and published under the payload
// contract in docs/ARCHITECTURE.md §5. The forwarder does not know or care
// whether these bytes came from this board or from sim/sensor_sim.py.
//
// Time handling is the part worth reading: SNTP disciplines the *epoch*, the
// hardware timer paces the *samples*, and the two are kept apart (§6.3).

#include <M5Unified.h>
#include <WiFi.h>
#include <MQTT.h>
#include <esp_sntp.h>
#include <sys/time.h>
#include <math.h>

#include "config.h"
#ifdef CPS_USE_TLS
#include <WiFiClientSecure.h>
#include "ca_cert.h"
#endif

// ---------------------------------------------------------------- contract --
static const int      SAMPLE_HZ = 50;
static const int      BATCH_N   = 50;                      // one second
static const uint32_t PERIOD_US = 1000000UL / SAMPLE_HZ;   // 20 ms
static const char    *FW_VERSION = "hw1-0.2.0";

// Microphone. 16 kHz x 320 samples = exactly 20 ms, so one audio block lines up
// with one IMU sample and both streams share a timebase with no resampling.
//
// Raw audio is deliberately NOT published: 16000 samples/s is 320x the IMU rate
// and would swamp the broker and InfluxDB for no analytical gain. Reducing to
// RMS and peak on the device is the whole point of edge processing in a CPS.
static const uint32_t MIC_RATE  = 16000;
static const size_t   MIC_BLOCK = 320;

// ------------------------------------------------------------------ state --
static float    ax_[BATCH_N], ay_[BATCH_N], az_[BATCH_N];
static float    gx_[BATCH_N], gy_[BATCH_N], gz_[BATCH_N];
static float    mrms_[BATCH_N], mpk_[BATCH_N];
static int      n_ = 0;
static double   batch_start_ts = 0;
static uint32_t seq_ = 0;
static uint32_t last_sample_us = 0;
static uint32_t published_ = 0, failed_ = 0;

static int16_t  mic_buf[MIC_BLOCK];
static bool     mic_ok = false;
static float    mic_rms_db = -90.0f;    // latest block, dBFS
static float    mic_peak   = 0.0f;      // latest block, 0..1

static char device_id[20];
static char topic_imu[72], topic_audio[72], topic_health[72], topic_status[72];

// ~3 KB of JSON per IMU batch; the MQTT buffer below must exceed this.
static char payload[7168];

#ifdef CPS_USE_TLS
static WiFiClientSecure net;        // validates the broker against CPS_CA_CERT
#else
static WiFiClient       net;
#endif
static MQTTClient  mqtt(8192);

// SNTP bookkeeping. last_step_ms is the correction SNTP actually applied on its
// most recent sync -- a real measurement, unlike the simulator's fabricated
// value, and what makes the Grafana skew panel mean something.
static volatile int64_t last_step_ms = 0;
static volatile time_t  last_sync_s  = 0;

static void onTimeSync(struct timeval *tv) {
  int64_t before = (int64_t)time(nullptr) * 1000;
  int64_t after  = (int64_t)tv->tv_sec * 1000 + tv->tv_usec / 1000;
  last_step_ms = after - before;
  last_sync_s  = tv->tv_sec;
  Serial.printf("[sntp] synced, step %lld ms\n", (long long)last_step_ms);
}

// ------------------------------------------------------------------ helpers --
static double nowEpoch() {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  return (double)tv.tv_sec + (double)tv.tv_usec / 1e6;
}

// Append "name":[v0,v1,...] and return characters written.
static int appendArray(char *p, size_t cap, const char *name,
                       const float *v, int n, int prec) {
  int w = snprintf(p, cap, "\"%s\":[", name);
  for (int i = 0; i < n && w < (int)cap; i++) {
    w += snprintf(p + w, cap - w, "%.*f%s", prec, v[i], (i < n - 1) ? "," : "");
  }
  w += snprintf(p + w, cap - w, "]");
  return w;
}

// ---------------------------------------------------------------- microphone --
// Reduce one block to RMS in dBFS and peak amplitude. Called once per completed
// DMA block, never per sample, so the cost is one logf per 20 ms.
static void reduceMicBlock(const int16_t *buf, size_t n) {
  double sumsq = 0;
  int32_t peak = 0;
  for (size_t i = 0; i < n; i++) {
    int32_t s = buf[i];
    sumsq += (double)s * (double)s;
    int32_t a = s < 0 ? -s : s;
    if (a > peak) peak = a;
  }
  float rms = sqrtf((float)(sumsq / (double)n));
  // dBFS relative to full scale. Silence floors near -90; a raised voice at
  // arm's length typically lands between -40 and -20.
  mic_rms_db = (rms > 1.0f) ? 20.0f * log10f(rms / 32768.0f) : -90.0f;
  mic_peak   = (float)peak / 32768.0f;
}

// Non-blocking: kick off the next DMA capture and reduce the previous one when
// it lands. Blocking on record() here would stall the 50 Hz IMU tick.
static void pumpMic() {
  if (!mic_ok) return;
  if (M5.Mic.isRecording()) return;
  reduceMicBlock(mic_buf, MIC_BLOCK);
  M5.Mic.record(mic_buf, MIC_BLOCK, MIC_RATE);
}

static void drawStatus(const char *state) {
  M5.Display.fillScreen(TFT_BLACK);
  M5.Display.setCursor(0, 0);
  M5.Display.setTextColor(TFT_GREEN, TFT_BLACK);
  M5.Display.printf("%s\n", device_id);
  M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
  M5.Display.printf("%s\n", state);
  M5.Display.printf("%d dBm\n", (int)WiFi.RSSI());
  M5.Display.printf("sent %lu\n", (unsigned long)published_);
  M5.Display.setTextColor(mic_ok ? TFT_CYAN : TFT_DARKGREY, TFT_BLACK);
  M5.Display.printf("mic %.0fdB\n", mic_rms_db);
  if (failed_) {
    M5.Display.setTextColor(TFT_RED, TFT_BLACK);
    M5.Display.printf("fail %lu\n", (unsigned long)failed_);
  }
}

// -------------------------------------------------------------- networking --
static void connectWiFi() {
  Serial.printf("[wifi] joining \"%s\" ...\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  M5.Display.printf("wifi..");
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
    M5.Display.print(".");
    if (millis() - t0 > 20000) {
      // The single most common failure, and it is silent otherwise: a 5 GHz-only
      // SSID simply does not exist as far as an ESP32 is concerned.
      Serial.println("[wifi] FAILED after 20s. Check: is the SSID 2.4 GHz? "
                     "Is the password right? Is the network WPA2-Enterprise?");
      t0 = millis();
    }
  }
  Serial.printf("[wifi] connected, ip=%s rssi=%d dBm\n",
                WiFi.localIP().toString().c_str(), (int)WiFi.RSSI());
  M5.Display.printf("\n%s\n", WiFi.localIP().toString().c_str());
}

static void startSNTP() {
  Serial.printf("[sntp] server %s\n", CPS_SERVER_IP);
  sntp_set_time_sync_notification_cb(onTimeSync);
  sntp_set_sync_interval(15 * 60 * 1000);
  configTime(0, 0, CPS_SERVER_IP);          // UTC; offsets belong in Grafana

  M5.Display.printf("sntp..");
  for (int i = 0; i < 40 && time(nullptr) < 1700000000; i++) {
    delay(500);
    M5.Display.print(".");
  }
  if (time(nullptr) < 1700000000) {
    Serial.println("[sntp] NO SYNC. Is chrony's `allow` covering this subnet? "
                   "Run server/setup/01_chrony.sh on the VM.");
  }
  M5.Display.printf("\n");
}

static void connectMQTT() {
  Serial.printf("[mqtt] %s:%d%s\n", CPS_SERVER_IP, CPS_MQTT_PORT,
#ifdef CPS_USE_TLS
                " (TLS)");
  net.setCACert(CPS_CA_CERT);        // reject any broker not signed by our CA
#else
                "");
#endif
  mqtt.begin(CPS_SERVER_IP, CPS_MQTT_PORT, net);
  mqtt.setKeepAlive(60);
  // Last Will: if this board drops off ungracefully the broker announces it,
  // so the dashboard's liveness panel stays truthful without polling.
  mqtt.setWill(topic_status, "offline", true, 1);

  int tries = 0;
  while (!mqtt.connect(device_id)) {
    if (++tries % 5 == 0) {
      Serial.printf("[mqtt] still refused after %d tries "
                    "(broker reachable? allow_anonymous set?)\n", tries);
    }
    delay(1000);
  }
  mqtt.publish(topic_status, "online", true, 1);
  Serial.println("[mqtt] connected");
}

// ------------------------------------------------------------------ publish --
static void publishBatch() {
  int32_t vbat_mv = M5.Power.getBatteryVoltage();

  // ---- IMU stream ----
  int w = snprintf(payload, sizeof(payload),
      "{\"device_id\":\"%s\",\"stream\":\"imu\",\"start_ts\":%.4f,"
      "\"freq\":%d,\"n\":%d,\"seq\":%lu,\"fw\":\"%s\",\"site\":\"%s\","
      "\"units\":{\"ax\":\"g\",\"gx\":\"dps\"},\"values\":{",
      device_id, batch_start_ts, SAMPLE_HZ, BATCH_N,
      (unsigned long)seq_, FW_VERSION, CPS_SITE);

  w += appendArray(payload + w, sizeof(payload) - w, "ax", ax_, BATCH_N, 5);
  w += snprintf(payload + w, sizeof(payload) - w, ",");
  w += appendArray(payload + w, sizeof(payload) - w, "ay", ay_, BATCH_N, 5);
  w += snprintf(payload + w, sizeof(payload) - w, ",");
  w += appendArray(payload + w, sizeof(payload) - w, "az", az_, BATCH_N, 5);
  w += snprintf(payload + w, sizeof(payload) - w, ",");
  w += appendArray(payload + w, sizeof(payload) - w, "gx", gx_, BATCH_N, 4);
  w += snprintf(payload + w, sizeof(payload) - w, ",");
  w += appendArray(payload + w, sizeof(payload) - w, "gy", gy_, BATCH_N, 4);
  w += snprintf(payload + w, sizeof(payload) - w, ",");
  w += appendArray(payload + w, sizeof(payload) - w, "gz", gz_, BATCH_N, 4);

  int32_t age = last_sync_s ? (int32_t)(time(nullptr) - last_sync_s) : -1;
  w += snprintf(payload + w, sizeof(payload) - w,
      "},\"meta\":{\"rssi\":%d,\"vbat\":%.2f,\"clock_src\":\"sntp\","
      "\"skew_ms\":%lld,\"sntp_age_s\":%ld}}",
      (int)WiFi.RSSI(), vbat_mv / 1000.0f,
      (long long)last_step_ms, (long)age);

  if (w > 0 && w < (int)sizeof(payload) &&
      mqtt.publish(topic_imu, payload, w, false, 1)) {
    published_++;
  } else {
    failed_++;
  }

  // ---- audio stream ----
  if (mic_ok) {
    int a = snprintf(payload, sizeof(payload),
        "{\"device_id\":\"%s\",\"stream\":\"audio\",\"start_ts\":%.4f,"
        "\"freq\":%d,\"n\":%d,\"seq\":%lu,\"fw\":\"%s\",\"site\":\"%s\","
        "\"units\":{\"rms_db\":\"dBFS\",\"peak\":\"norm\"},\"values\":{",
        device_id, batch_start_ts, SAMPLE_HZ, BATCH_N,
        (unsigned long)seq_, FW_VERSION, CPS_SITE);
    a += appendArray(payload + a, sizeof(payload) - a, "rms_db", mrms_, BATCH_N, 2);
    a += snprintf(payload + a, sizeof(payload) - a, ",");
    a += appendArray(payload + a, sizeof(payload) - a, "peak", mpk_, BATCH_N, 4);
    a += snprintf(payload + a, sizeof(payload) - a,
        "},\"meta\":{\"mic_rate\":%lu,\"block\":%u}}",
        (unsigned long)MIC_RATE, (unsigned)MIC_BLOCK);

    if (a > 0 && a < (int)sizeof(payload)) {
      mqtt.publish(topic_audio, payload, a, false, 1);
    }
  }

  // ---- health, at 1/10 the waveform rate ----
  if (seq_ % 10 == 0) {
    int h = snprintf(payload, sizeof(payload),
        "{\"device_id\":\"%s\",\"stream\":\"health\",\"start_ts\":%.4f,"
        "\"site\":\"%s\",\"fw\":\"%s\",\"values\":{\"vbat\":%.2f,\"rssi\":%d,"
        "\"heap_free\":%lu,\"uptime_s\":%lu}}",
        device_id, nowEpoch(), CPS_SITE, FW_VERSION,
        vbat_mv / 1000.0f, (int)WiFi.RSSI(),
        (unsigned long)ESP.getFreeHeap(),
        (unsigned long)(millis() / 1000));
    mqtt.publish(topic_health, payload, h, false, 1);
    drawStatus("streaming");
    Serial.printf("[pub] seq=%lu sent=%lu fail=%lu mic=%.1f dBFS "
                  "rssi=%d heap=%lu\n",
                  (unsigned long)seq_, (unsigned long)published_,
                  (unsigned long)failed_, mic_rms_db,
                  (int)WiFi.RSSI(), (unsigned long)ESP.getFreeHeap());
  }

  seq_++;
}

// --------------------------------------------------------------------- main --
void setup() {
  auto cfg = M5.config();
  M5.begin(cfg);

  // Serial logging: the firmware used to report only to the LCD, which made a
  // failed WiFi join indistinguishable from a dead board. Every failure path
  // now says what to check.
  Serial.begin(115200);
  delay(300);
  Serial.println("\n\n=== CPS IoT — M5StickC PLUS ===");
  Serial.printf("fw %s, build %s %s\n", FW_VERSION, __DATE__, __TIME__);

  M5.Display.setRotation(3);
  M5.Display.setTextSize(2);
  M5.Display.fillScreen(TFT_BLACK);
  M5.Display.setCursor(0, 0);

  // Stable identity with no provisioning server: the MAC's last three bytes.
  uint8_t mac[6];
  WiFi.macAddress(mac);
  snprintf(device_id, sizeof(device_id), "m5-%02x%02x%02x", mac[3], mac[4], mac[5]);
  snprintf(topic_imu,    sizeof(topic_imu),    "cps/hw1/%s/imu",    device_id);
  snprintf(topic_audio,  sizeof(topic_audio),  "cps/hw1/%s/audio",  device_id);
  snprintf(topic_health, sizeof(topic_health), "cps/hw1/%s/health", device_id);
  snprintf(topic_status, sizeof(topic_status), "cps/hw1/%s/status", device_id);
  Serial.printf("[id] %s\n", device_id);

  mic_ok = M5.Mic.begin();
  Serial.printf("[mic] %s\n", mic_ok ? "SPM1423 ready" : "UNAVAILABLE");
  if (mic_ok) M5.Mic.record(mic_buf, MIC_BLOCK, MIC_RATE);

  connectWiFi();
  startSNTP();
  connectMQTT();

  drawStatus("streaming");
  last_sample_us = micros();
  Serial.println("[run] streaming imu + audio at 50 Hz");
}

void loop() {
  M5.update();
  mqtt.loop();
  pumpMic();

  if (!mqtt.connected()) {
    Serial.println("[mqtt] lost, reconnecting");
    drawStatus("reconnecting");
    connectMQTT();
    last_sample_us = micros();
    n_ = 0;
  }

  // The sample clock is the hardware timer, NOT wall clock. An SNTP step must
  // never reorder or duplicate samples inside a batch, so gettimeofday() is
  // called exactly once per batch and per-sample times are derived downstream
  // as start_ts + i/freq.
  uint32_t now = micros();
  if ((uint32_t)(now - last_sample_us) >= PERIOD_US) {
    last_sample_us += PERIOD_US;

    if (n_ == 0) batch_start_ts = nowEpoch();

    float ax, ay, az, gx, gy, gz;
    M5.Imu.getAccel(&ax, &ay, &az);
    M5.Imu.getGyro(&gx, &gy, &gz);

    ax_[n_] = ax; ay_[n_] = ay; az_[n_] = az;
    gx_[n_] = gx; gy_[n_] = gy; gz_[n_] = gz;
    mrms_[n_] = mic_rms_db; mpk_[n_] = mic_peak;

    if (++n_ >= BATCH_N) {
      publishBatch();
      n_ = 0;
    }
  }
}
