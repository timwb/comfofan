#!/usr/bin/env python3
"""Interactive control for the Pi 0-10 V output.

Hardware: GPIO18 (pin 12) -> 4N35 -> GP8101 PWM input, 1 kHz hardware PWM
          GPIO23 (pin 16) -> BC547C -> relay K1 (high = DAC, low = divider)

Requires the pigpio daemon:  sudo systemctl enable --now pigpiod
"""

import sys
import time

import pigpio

PWM_PIN = 18        # physical pin 12, hardware PWM
RELAY_PIN = 23      # physical pin 16
PWM_FREQ = 1000     # Hz
SETTLE_S = 0.1      # GP8101 power-up and lock-on time after selecting the DAC
STEP_V = 0.1        # default step for + and -

HELP = """
Commands:
  <number>    set DAC output voltage, e.g. 4.5  (0-10 V)
  + [step]    raise voltage by step (default 0.1 V), e.g. + 0.5
  - [step]    lower voltage by step (default 0.1 V)
  dac         select the DAC as output source
  div         select the 10k/10k divider as output source
  s           show status
  h           show this help
  q           quit (selects the divider and sets PWM to 0)
"""


class Output:
  def __init__(self, pi):
    self.pi = pi
    self.volts = 0.0
    self.use_dac = False
    pi.set_mode(RELAY_PIN, pigpio.OUTPUT)
    self.set_volts(0.0)
    self.select(False)

  def set_volts(self, volts):
    volts = max(0.0, min(10.0, volts))
    duty = int(round(volts / 10.0 * 1_000_000))
    self.pi.hardware_PWM(PWM_PIN, PWM_FREQ, duty)
    self.volts = volts

  def select(self, use_dac):
    self.pi.write(RELAY_PIN, 1 if use_dac else 0)
    if use_dac and not self.use_dac:
      time.sleep(SETTLE_S)
    self.use_dac = use_dac

  def status(self):
    source = "DAC" if self.use_dac else "divider (~4.8 V into 20k)"
    return (f"Source: {source} | DAC setpoint: {self.volts:.2f} V "
            f"(duty {self.volts * 10:.1f}%)")

  def shutdown(self):
    self.select(False)
    self.set_volts(0.0)


def parse_step(parts):
  if len(parts) == 1:
    return STEP_V
  return float(parts[1])


def main():
  pi = pigpio.pi()
  if not pi.connected:
    print("Cannot connect to the pigpio daemon. Start it with: sudo pigpiod")
    sys.exit(1)

  out = Output(pi)
  print(HELP)
  print(out.status())

  try:
    while True:
      try:
        line = input("> ").strip().lower()
      except EOFError:
        break
      if not line:
        continue
      parts = line.split()
      cmd = parts[0]

      try:
        if cmd in ("q", "quit", "exit"):
          break
        elif cmd in ("h", "help", "?"):
          print(HELP)
          continue
        elif cmd in ("s", "status"):
          pass
        elif cmd == "dac":
          out.select(True)
        elif cmd == "div":
          out.select(False)
        elif cmd == "+":
          out.set_volts(out.volts + parse_step(parts))
        elif cmd == "-":
          out.set_volts(out.volts - parse_step(parts))
        else:
          requested = float(cmd)
          if not 0.0 <= requested <= 10.0:
            print("Voltage must be between 0 and 10. Clamped.")
          out.set_volts(requested)
      except ValueError:
        print(f"Unknown command: {line!r}. Type h for help.")
        continue

      print(out.status())
  except KeyboardInterrupt:
    print()
  finally:
    out.shutdown()
    pi.stop()
    print("Divider selected, PWM off. Bye.")


if __name__ == "__main__":
  main()
