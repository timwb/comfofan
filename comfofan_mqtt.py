#!/usr/bin/env python3
"""Home Assistant MQTT fan for the Stork Comfofan S, driven by the Pi 0-10 V output.

Hardware: GPIO18 (pin 12) -> 4N35 -> GP8101 PWM input, 1 kHz hardware PWM
          GPIO23 (pin 16) -> BC547C -> relay K1 (high = DAC, low = divider)

Home Assistant behaviour:
  fan ON at N %  -> relay on DAC, output N/10 V (100 % = 10 V)
  fan OFF        -> relay on the divider (the unit's hardware default speed)

Fallback to the divider happens on:
  - a normal stop (systemctl stop, SIGTERM, Ctrl+C) or a Python exception: finally block
  - a hard crash or kill: systemd ExecStopPost (pinctrl, independent of pigpio)
  - a hang: systemd watchdog kills the process, then ExecStopPost runs
  - power loss or reboot: R3 holds Q1 off, so the relay rests on the divider

Settings come from environment variables (see comfofan.env).
"""

import json
import logging
import os
import signal
import socket
import sys
import threading
import time

import paho.mqtt.client as mqtt
import pigpio

# ---------------------------------------------------------------- hardware
PWM_PIN = 18        # physical pin 12, hardware PWM
RELAY_PIN = 23      # physical pin 16
PWM_FREQ = 1000     # Hz
SETTLE_S = 0.1      # GP8101 power-up and lock-on time after selecting the DAC
DEFAULT_PCT = 50    # used for "turn on" when no earlier speed is known

# ---------------------------------------------------------------- MQTT
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER") or None
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD") or None
DISCOVERY_PREFIX = os.environ.get("HA_DISCOVERY_PREFIX", "homeassistant")

DEVICE_ID = "stork_comfofan_s"
DEVICE_NAME = "Stork Comfofan S Ventilation Unit"
BASE = f"comfofan/{DEVICE_ID}"

T_AVAIL = f"{BASE}/availability"
T_STATE = f"{BASE}/state"
T_CMD = f"{BASE}/set"
T_PCT_STATE = f"{BASE}/percentage"
T_PCT_CMD = f"{BASE}/percentage/set"
T_DISCOVERY = f"{DISCOVERY_PREFIX}/fan/{DEVICE_ID}/config"
T_HA_STATUS = f"{DISCOVERY_PREFIX}/status"

log = logging.getLogger("comfofan")


# ---------------------------------------------------------------- systemd
def sd_notify(message):
    """Send a message to systemd (READY=1, WATCHDOG=1). No-op outside systemd."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())
    except OSError as exc:
        log.warning("sd_notify failed: %s", exc)


def watchdog_interval():
    """Half the systemd watchdog timeout, or 10 s when no watchdog is set."""
    usec = os.environ.get("WATCHDOG_USEC")
    if usec:
        return int(usec) / 1_000_000 / 2
    return 10.0


# ---------------------------------------------------------------- output
class Output:
    """Relay and PWM control. Thread-safe: MQTT callbacks run in their own thread."""

    def __init__(self, pi):
        self.pi = pi
        self.lock = threading.Lock()
        self.on = False
        self.pct = 0
        self.last_pct = DEFAULT_PCT
        pi.set_mode(RELAY_PIN, pigpio.OUTPUT)
        self.turn_off()

    def _select_divider(self):
        self.pi.write(RELAY_PIN, 0)
        self.pi.hardware_PWM(PWM_PIN, PWM_FREQ, 0)
        self.on = False
        self.pct = 0

    def turn_off(self):
        with self.lock:
            self._select_divider()
        log.info("Divider selected")

    def turn_on(self):
        self.set_percentage(self.last_pct)

    def set_percentage(self, pct):
        pct = max(0, min(100, int(pct)))
        if pct == 0:
            self.turn_off()
            return
        with self.lock:
            # Set the duty first, so the GP8101 powers up straight into the right value.
            self.pi.hardware_PWM(PWM_PIN, PWM_FREQ, pct * 10_000)
            if not self.on:
                self.pi.write(RELAY_PIN, 1)
                time.sleep(SETTLE_S)
            self.on = True
            self.pct = pct
            self.last_pct = pct
        log.info("DAC selected at %d %% (%.1f V)", pct, pct / 10)

    def check(self):
        """Raise if the pigpio daemon is gone; re-assert the relay if it drifted."""
        expected = 1 if self.on else 0
        with self.lock:
            if self.pi.read(RELAY_PIN) != expected:
                log.warning("Relay pin out of sync, rewriting")
                self.pi.write(RELAY_PIN, expected)


# ---------------------------------------------------------------- Home Assistant
def discovery_payload():
    return {
        "name": None,  # entity takes the device name
        "unique_id": f"{DEVICE_ID}_fan",
        "availability_topic": T_AVAIL,
        "command_topic": T_CMD,
        "state_topic": T_STATE,
        "percentage_command_topic": T_PCT_CMD,
        "percentage_state_topic": T_PCT_STATE,
        "icon": "mdi:hvac",
        "device": {
            "identifiers": [DEVICE_ID],
            "name": DEVICE_NAME,
            "manufacturer": "Stork",
            "model": "Comfofan S",
        },
    }


def publish_state(client, out):
    client.publish(T_STATE, "ON" if out.on else "OFF", retain=True)
    client.publish(T_PCT_STATE, str(out.pct), retain=True)


def announce(client, out):
    client.publish(T_DISCOVERY, json.dumps(discovery_payload()), retain=True)
    client.publish(T_AVAIL, "online", retain=True)
    publish_state(client, out)


def reason_value(reason_code):
    return getattr(reason_code, "value", reason_code)


def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_value(reason_code) != 0:
        log.error("MQTT connection refused: %s", reason_code)
        return
    log.info("Connected to MQTT broker %s:%d", MQTT_HOST, MQTT_PORT)
    client.subscribe([(T_CMD, 1), (T_PCT_CMD, 1), (T_HA_STATUS, 1)])
    announce(client, userdata["out"])


def on_disconnect(client, userdata, *args):
    log.warning("Disconnected from MQTT broker, retrying. Output stays as it is.")


def on_message(client, userdata, msg):
    out = userdata["out"]
    payload = msg.payload.decode(errors="replace").strip()

    if msg.topic == T_HA_STATUS:
        if payload == "online":  # Home Assistant restarted
            announce(client, out)
        return

    if msg.retain:
        log.info("Ignoring retained command on %s", msg.topic)
        return

    try:
        if msg.topic == T_CMD:
            command = payload.upper()
            if command == "ON":
                out.turn_on()
            elif command == "OFF":
                out.turn_off()
            else:
                log.warning("Unknown command %r", payload)
                return
        elif msg.topic == T_PCT_CMD:
            out.set_percentage(int(float(payload)))
    except ValueError:
        log.warning("Invalid payload %r on %s", payload, msg.topic)
        return

    publish_state(client, out)


def make_client(out):
    try:  # paho-mqtt 2.x
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=DEVICE_ID)
    except AttributeError:  # paho-mqtt 1.x (Raspberry Pi OS package)
        client = mqtt.Client(client_id=DEVICE_ID)
    client.user_data_set({"out": out})
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.will_set(T_AVAIL, "offline", retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    return client


# ---------------------------------------------------------------- main
def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    pi = pigpio.pi()
    if not pi.connected:
        log.error("Cannot connect to the pigpio daemon (is pigpiod running?)")
        sys.exit(1)

    out = Output(pi)
    client = make_client(out)

    stop = threading.Event()

    def request_stop(signum, frame):
        log.info("Received signal %d, stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
        client.loop_start()
        sd_notify("READY=1")

        interval = watchdog_interval()
        while not stop.wait(interval):
            out.check()  # raises if pigpiod is gone -> exit -> systemd restarts us
            sd_notify("WATCHDOG=1")
    finally:
        # Hardware first: this is the part that matters if anything below fails.
        try:
            out.turn_off()
        except Exception as exc:  # pigpiod gone; ExecStopPost will handle it
            log.error("Could not reset output: %s", exc)
        try:
            info = client.publish(T_AVAIL, "offline", retain=True)
            info.wait_for_publish(timeout=2)
        except Exception:
            pass
        client.disconnect()
        client.loop_stop()
        pi.stop()
        log.info("Stopped, divider selected")


if __name__ == "__main__":
    main()
