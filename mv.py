#!/usr/bin/env python3
import time
import pigpio

pi = pigpio.pi()
PWM_PIN = 18     # physical pin 12, hardware PWM
RELAY_PIN = 23   # physical pin 16
pi.set_mode(RELAY_PIN, pigpio.OUTPUT)

def set_output(volts):
    duty = volts / 10.0
    pi.hardware_PWM(PWM_PIN, 1000, int(duty * 1_000_000))  # 1 kHz; duty is 0–1,000,000

def select_source(use_dac: bool):
    pi.write(RELAY_PIN, 1 if use_dac else 0)
    if use_dac:
        time.sleep(0.1)   # GP8101 power-up and lock-on

set_output(5.0)        # set the duty first
select_source(True)    # switch the output to the DAC
