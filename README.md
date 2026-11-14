# comfofan

Control the speed of a **Zehnder / Stork Comfofan S** ventilation unit from Home Assistant, using a
Raspberry Pi that drives the unit's 0–10 V speed input, electrically isolated, over MQTT.

A relay picks the signal that goes to the unit:

- **DAC** (relay on): a PWM-to-0–10 V module sets the speed that Home Assistant asks for.
- **Divider** (relay off): a fixed resistor divider or 20k potentiometer gives the unit's normal
  default speed (about 4.8 V).

The relay is off when nothing powers it. If the Pi, the script or the power fails, the unit falls
back to its default speed. It never stops. The Pi side and the 12 V side are galvanically
isolated.

> **Status:** the circuit works on a breadboard. The scripts have been tested only against
> stand-ins for pigpio and MQTT, not yet on a real Pi. See [Open items](#open-items).

## How it works

```
Raspberry Pi                    │ isolation │                12 V side
                                │           │
GPIO18 ── 1 kHz PWM ──────────── 4N35 opto ───── GP8101 PWM→0–10 V ──┐
                                │           │                         NO
                                │           │                          ├── COM ──► Comfofan S
GPIO23 ── BC547C ── relay coil ─────── K1 ───── 10k/10k divider ─────┘   0–10 V input
                                │           │                         NC
```

K1 is a double-pole relay. Pole 1 switches the signal to the unit. Pole 2 powers either the
DAC or the divider, so only the active source draws current from the 12 V supply.

The full schematic, bill of materials and reference tables are in
[`Pi 0–10 V Output.html`](Pi%200–10%20V%20Output.html). Open it in a browser.

## Why

I have one of these at home and want to control it remotely based on other inputs in Home Assistant,
Such as indoor air quality, outdoor temperature, heatpump activity, etc.

## Warnings

- **Never connect Pi GND to 12 V GND.** U1 and K1 are the only parts that cross the isolation
barrier.
- The GP8101 accepts a PWM high level of 3.0–5.5 V only. Never feed its PWM input 12 V.
- The 12 V side draws about 0.7 mA with the divider selected and about 3.3 mA with the DAC selected.
  A comfofan S can source 20mA.

## Raspberry Pi setup

This project was designed for a **Raspberry Pi**. It currently uses the pigpio daemon.

1. Install the pigpio daemon and enable it:

   ```sh
   sudo apt install pigpio
   sudo systemctl enable --now pigpiod
   ```

2. Turn off analog audio, which shares the PWM hardware with GPIO18. In
   `/boot/firmware/config.txt`:

   ```
   dtparam=audio=off
   ```

   Do **not** add `dtoverlay=pwm`. The kernel PWM driver and pigpio would fight over the same
   hardware.

3. Reboot.

## Running in a virtual environment

The Python dependencies are listed in [`requirements.txt`](requirements.txt): the pigpio client
library and paho-mqtt (1.x or 2.x). Install them in a virtual environment, not system-wide.
The pigpio daemon itself comes from apt (see [Raspberry Pi setup](#raspberry-pi-setup)).

To try the scripts by hand from a clone of this repository:

```sh
sudo apt install python3-venv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Then run any script with the venv's Python. You don't need to activate the venv first:

```sh
.venv/bin/python mv-interactive.py
```

To run the service in the foreground, pass the broker settings as environment variables:

```sh
MQTT_HOST=localhost MQTT_USER=comfofan MQTT_PASSWORD=change-me \
  .venv/bin/python comfofan_mqtt.py
```

Press Ctrl+C to stop it. The script selects the divider on the way out. Stop the systemd
service first if it is installed (`sudo systemctl stop comfofan`), so the two don't fight over
the pins.

## Installation

The systemd unit expects the script and a virtual environment in
`/opt/comfofan/`, running as the user `homeautomation`.

```sh
sudo mkdir -p /opt/comfofan /etc/homeautomation
sudo cp comfofan_mqtt.py requirements.txt /opt/comfofan/
sudo python3 -m venv /opt/comfofan/.venv
sudo /opy/comfofan/.venv/bin/pip install \
  -r /opt/comfofan/requirements.txt
```

The unit starts the script with `/opt/comfofan/.venv/bin/python`, so it
always uses this venv.

To update later, copy the new `comfofan_mqtt.py` (and `requirements.txt`, if it changed), run the
`pip install` line again and restart: `sudo systemctl restart comfofan`.

### Configuration

Create `/etc/homeautomation/comfofan.env` with your broker settings:

```sh
MQTT_HOST=localhost
MQTT_PORT=1883
MQTT_USER=comfofan
MQTT_PASSWORD=change-me
HA_DISCOVERY_PREFIX=homeassistant
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `MQTT_HOST` | `localhost` | MQTT broker host |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USER` | none | Broker user name. Leave empty for no login |
| `MQTT_PASSWORD` | none | Broker password |
| `HA_DISCOVERY_PREFIX` | `homeassistant` | Home Assistant MQTT discovery prefix |

The file holds a password, so make it readable by root only. systemd reads it as root, so the
service user does not need access:

```sh
sudo chown root:root /etc/homeautomation/comfofan.env
sudo chmod 600 /etc/homeautomation/comfofan.env
```

### Start the service

```sh
sudo cp comfofan.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now comfofan
journalctl -u comfofan -f
```

The unit calls `/usr/bin/pinctrl` when it stops (see [Fail-safe](#fail-safe)). Check the path
with `which pinctrl`. On Raspberry Pi OS Bullseye or older, replace those lines with
`raspi-gpio set 23 op dl` and `raspi-gpio set 18 op dl`.

## Home Assistant

The service registers itself through MQTT discovery. A fan entity appears on the device
**Stork Comfofan S Ventilation Unit**.

| Action in Home Assistant | Result |
| --- | --- |
| Fan on at N % | DAC selected, output N/10 V (100 % = 10 V) |
| Fan on, no percentage | Resumes the last speed (50 % after a restart) |
| Fan off, or 0 % | Divider selected: the unit runs at its default speed |

"Off" does not stop the ventilation. It returns the unit to its hardware default.

MQTT topics, under `comfofan/stork_comfofan_s/`:

| Topic | Direction | Payload |
| --- | --- | --- |
| `set` | Command | `ON` / `OFF` |
| `percentage/set` | Command | `0`–`100` |
| `state` | State | `ON` / `OFF` |
| `percentage` | State | `0`–`100` |
| `availability` | State | `online` / `offline` (last will) |

Discovery is published to `homeassistant/fan/stork_comfofan_s/config`. The service publishes
discovery and state again when Home Assistant restarts. It ignores retained command messages.
If the broker connection drops, the output keeps its current setting while the client
reconnects.

## Fail-safe

The output must fall back to the divider whenever the software is not in control. The pigpio
daemon keeps driving the pins after the script dies, so the project has four layers of
protection:

1. **Normal stop, SIGTERM or Python exception:** the script selects the divider and publishes
   `offline`.
2. **Hard crash or kill:** `ExecStopPost` in the systemd unit uses `pinctrl` to drive GPIO23
   and GPIO18 low. This works even if pigpiod is not responding.
3. **Hang:** the script pings the systemd watchdog every 15 s, after checking that pigpiod still
   answers. If it stops, systemd kills it after 30 s, layer 2 runs, and the service restarts
   after 5 s.
4. **Power loss or reboot:** R3 holds the relay transistor off.

## Bench tools

Both tools need the pigpio daemon. Run them with the venv's Python
(`.venv/bin/python mv-interactive.py`, see
[Running in a virtual environment](#running-in-a-virtual-environment)). Stop the service first
(`sudo systemctl stop comfofan`) so they don't fight over the pins.

- **`mv-interactive.py`:** interactive CLI to set and check the output by hand. It selects the
  divider when you quit.

  | Command | Action |
  | --- | --- |
  | `4.5` | Set the DAC to 4.5 V (0–10) |
  | `+` / `-` `[step]` | Raise or lower by a step (default 0.1 V) |
  | `dac` / `div` | Select the DAC or the divider |
  | `s` | Show status |
  | `h` | Help |
  | `q` | Quit |

- **`mv.py`:** minimal example that sets 5 V and selects the DAC. It leaves the relay on when it
  exits.

Always set the PWM duty before you switch the relay to the DAC, so the GP8101 powers up at the
right value.

### Measuring

Use a meter with a high input impedance (10 MΩ) on `PWM_IN` and on the R6/R7 divider. A
low-impedance meter loads these nodes and shows readings that are too low. On the 1 kHz PWM a
meter shows the average, so measure the levels at 0 % and 100 % duty.

## Open items

- Have a non-pigpiod mode with kernel PWM for Pi 5 support
- Support 1-10V in addition to 0-10V

## License

Apache License 2.0. See [LICENSE](LICENSE).
