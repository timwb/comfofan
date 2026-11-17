# comfofan

Control the speed of a **Zehnder / Stork Comfofan S** ventilation unit from Home Assistant, using a
Raspberry Pi that drives the unit's 0–10 V (or 1–10 V) speed input, electrically isolated, over MQTT with Home Assistant integration.

A relay picks the signal that goes to the unit:

- **DAC** (relay on): Home Assistant is in control. A PWM-to-0–10 V module sets the speed that
  Home Assistant asks for, or 0 V when Home Assistant turns the fan off.
- **Divider fallback** (relay off): Home Assistant is not in control. A fixed resistor divider or 20k
  potentiometer gives the unit's normal default speed.

The relay is off when nothing powers it. If the Pi, the script or the power fails, the unit falls
back to its default speed. It never stops. The Pi side and the 12 V side are galvanically
isolated.

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

The name `mv` refers to mechanical ventilation. But of course you can use this with any device that takes a 0-10v input with its own voltage source.

## Warnings

- **Never connect Pi GND to 12 V GND.** U1 and K1 are the only parts that cross the isolation
barrier.
- The GP8101 accepts a PWM high level of 3.0–5.5 V only. Never feed its PWM input 12 V.
- The 12 V side draws about 0.7 mA with the divider selected and about 3.3 mA with the DAC selected.
  A comfofan S can source 20mA.

## Requirements

- A Raspberry Pi <5. The pigpio daemon does not work on a Pi 5.
- The pigpio daemon (from apt) and an MQTT broker
- The Python packages in [`requirements.txt`](requirements.txt): the pigpio client library and
  paho-mqtt (1.x or 2.x), installed in a venv

## Raspberry Pi setup

1. Install the pigpio daemon and enable it:

   ```sh
   sudo apt install pigpio python3-venv
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

## Installation

In these examples everything lives under `/opt/comfofan/`, with its own virtual environment, owned by root:

```sh
cd ~/src/comfofan
sudo install -d /opt/comfofan
sudo cp comfofan_mqtt.py requirements.txt README.md /opt/comfofan/
sudo python3 -m venv /opt/comfofan/.venv
sudo /opt/comfofan/.venv/bin/pip install -r /opt/comfofan/requirements.txt
sudo chown -R root:root /opt/comfofan && sudo chmod -R go+rX /opt/comfofan
```

To update later, copy the new `comfofan_mqtt.py` (and `requirements.txt`, if it changed), run the
`pip install` line again and restart: `sudo systemctl restart comfofan`.

## Configuration

`comfofan_mqtt.py` has no command-line options. Its settings are environment variables: under
systemd they come from `/etc/homeautomation/comfofan.env` via `EnvironmentFile=`. Start from the
commented template:

```sh
sudo install -d -m 755 /etc/homeautomation
sudo install -m 600 -o root -g root comfofan.env.example /etc/homeautomation/comfofan.env
sudoedit /etc/homeautomation/comfofan.env
```

| Key | Default | Description |
| --- | --- | --- |
| `COMFOFAN_VOLTAGE_RANGE` | `0-10` | Speed input range of the unit: `0-10` or `1-10`, see [Home Assistant](#home-assistant). Any other value exits with code 2 |
| `MQTT_HOST` | `localhost` | MQTT broker host |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USERNAME` | empty | Broker user name. Leave empty for no login |
| `MQTT_PASSWORD` | empty | Broker password |
| `HA_DISCOVERY_PREFIX` | `homeassistant` | Home Assistant MQTT discovery prefix |

`MQTT_USER`, the old name of `MQTT_USERNAME`, still works but logs a warning. Rename it in the file.

The file holds a password, so it is readable by root only (`root:root 0600`). systemd reads it as
root, so the service user does not need access.

## systemd

```sh
sudo cp comfofan.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now comfofan
journalctl -u comfofan -f
```

What [`comfofan.service`](comfofan.service) does:

- **Base path:** `WorkingDirectory=/opt/comfofan`, and the unit starts `.venv/bin/python comfofan_mqtt.py`
  relative to it, so it always uses this venv. If you install elsewhere, change that line and the
  `Documentation=` line.
- **Unprivileged user:** `DynamicUser=yes` gives each start a transient user, so no system account
  is needed. The script reaches the pins through pigpiod (TCP on localhost:8888), so it needs no
  extra groups or device access.
- **Requires pigpiod:** it starts after `pigpiod.service`, and stopping pigpiod also stops it.
- **Watchdog and fail-safe:** `Type=notify` with a 30 s watchdog, and `ExecStopPost=` lines that drive
  the pins low after any stop. See [Fail-safe](#fail-safe).
- **Restarts:** after a failure, 5 s later. A configuration error (exit code 2, e.g. an invalid
  `COMFOFAN_VOLTAGE_RANGE`) is not restarted; the output stays on the divider.
- **Hardening:** a read-only filesystem, no home directories, no capabilities, a system call filter
  and more (`systemd-analyze security comfofan`). `PrivateDevices=` and `ProtectClock=` are
  deliberately off: they restrict device access for the whole unit, including the `ExecStopPost=`
  commands, so `pinctrl` could no longer reach the GPIO registers and the fail-safe would stop working.
- **Read-only install:** the service can't write `__pycache__` (`PYTHONDONTWRITEBYTECODE=1`).
  Optionally pre-compile after each install:
  `sudo /opt/comfofan/.venv/bin/python -m compileall -q /opt/comfofan`.

The unit calls `/usr/bin/pinctrl` when it stops. Check the path with `which pinctrl`. On Raspberry
Pi OS Bullseye or older, replace those lines with `raspi-gpio set 23 op dl` and
`raspi-gpio set 18 op dl`.

After installing, test the crash path: `sudo systemctl kill -s KILL comfofan` should drop the relay
immediately.

## Home Assistant

The service registers itself through MQTT discovery. A fan entity appears on the device
**Stork Comfofan S Ventilation Unit**.

| Action in Home Assistant | Relay | Output |
| --- | --- | --- |
| Fan on at N % | DAC | N % of the voltage range, see below |
| Fan on, no percentage | DAC | Resumes the last speed (50 % after a restart) |
| Fan off, or 0 % | DAC | 0 V: the fan is really off |
| Preset **Unit default** | Divider | The unit's hardware default speed; Home Assistant is not in control |

The preset hands control back to the unit. Any on, off or percentage command takes control again.
While the preset is active the fan shows as on, with an unknown percentage.

**The service always starts in Unit default** (divider), after a restart of the service or the Pi,
until Home Assistant sends a command. It also falls back to the divider whenever it stops or
fails, see [Fail-safe](#fail-safe).

Speed to voltage, set with `COMFOFAN_VOLTAGE_RANGE`. Only off gives 0 V:

| Range | 1 % | 50 % | 100 % | Formula (1–100 %) |
| --- | --- | --- | --- | --- |
| `0-10` (default) | 0.1 V | 5.0 V | 10 V | N / 10 V |
| `1-10` | 1.09 V | 5.5 V | 10 V | 1 + 9 × N / 100 V |

MQTT topics, under `comfofan/stork_comfofan_s/`:

| Topic | Direction | Payload |
| --- | --- | --- |
| `set` | Command | `ON` / `OFF` |
| `percentage/set` | Command | `0`–`100` |
| `preset/set` | Command | `Unit default` |
| `state` | State | `ON` / `OFF` |
| `percentage` | State | `0`–`100`, or `None` (unknown) in Unit default |
| `preset` | State | `Unit default`, or `None` while Home Assistant is in control |
| `availability` | State | `online` / `offline` (last will) |

Discovery is published to `homeassistant/fan/stork_comfofan_s/config`. The service publishes
discovery and state again when Home Assistant restarts. It ignores retained command messages.
If the broker connection drops, the output keeps its current setting while the client
reconnects.

## Fail-safe

The output must fall back to the divider whenever the software is not in control, so the unit
runs at its default speed. That includes the 0 V "off" state: if the software dies, the fan comes
back on at the default speed rather than staying off. The pigpio
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
[Development](#development)). Stop the service first
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

## Development

Use a venv in the source tree. You don't need to activate it; run scripts with the venv's Python:

```sh
cd ~/src/comfofan
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pylint comfofan_mqtt.py
```

To run the service in the foreground, pass the settings as environment variables:

```sh
MQTT_HOST=localhost MQTT_USERNAME=comfofan MQTT_PASSWORD=change-me \
  .venv/bin/python comfofan_mqtt.py
```

Press Ctrl+C to stop it. The script selects the divider on the way out. Stop the systemd
service first if it is installed (`sudo systemctl stop comfofan`), so the two don't fight over
the pins.

## Open items

- Have a non-pigpiod mode with kernel PWM for Pi 5 support

## License

Apache License 2.0. See [LICENSE](LICENSE).
