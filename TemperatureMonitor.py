#!/usr/bin/env python3
#
# TemperatureMonitory.py
#   Raspberrypi based refrigerator and freezer temperature monitor.
#
#Based on Adafruit's Raspberry Pi Lesson 11 Temperature sensing tutorial by Simon Monk
#    http://tinyurl.com/on5tpdr
#Modified by Tim Massaro 2/2014
#    http://tinyurl.com/zsntd48
#    This script now uses a Raspberry Pi, Adafruit PiPlate LCD and
#    two DS18B20 temp sensor to monitor the freezer and fridge unit
#    at Channel One Food Shelf
#Modified by Chuck Hunley 10/9/2016
#    Modified to use PiFace Control and Display 2.  The buttons are
#    event driven instead of polling like Adafruit PiPlate LCD
#    Modified to send an e-mail alert via smtp.gmail.com instead of text msg.
#
# License ?
#   No license.  I put this project out for sharing and learning.

#
# Import required modules
#
import signal
import sys
PY3 = sys.version_info[0] >= 3
if not PY3:
    print("TemperatureMonitor only works with 'python3'.")
    sys.exit(1)

import os
import glob
import time
import datetime

import smtplib
import email
from email.mime.text import MIMEText

try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO

from decimal import Decimal
global alertsent  # used to avoid sending too many alert emails
global toggledots # toggle the colon on LCD to indicate running
global updatesent # Daily update sent
alertsent  = 1    # Initialize to not send.  Button 1 enables
updatesent = 0
toggledots = 0
global lastsend
lastsend = -1

global lcdlock    # Lock to control write access to LCD

# init the acceptable temperature ranges - these will be overridden by config file
workLowFridge  = 30
workHiFridge   = 45
workLowFreezer = 30
workHiFreezer  = 45

#
# Import time and sleep module
#
import time
from time import sleep

#
# Import threading module
#
import threading
from threading import Barrier

#
# Import PiFace CaD modules
#
import pifacecommon
import pifacecad

from enum import Enum
from enum import IntEnum

#
# Enum for LCDStatus
#
class LCDStatus(Enum):
    OFF = 0
    ON  = 1

#
# Enum for LCD Buttons
#
class LCDButtons(IntEnum):
    Button0 = 0
    Button1 = 1
    Button2 = 2
    Button3 = 3
    Button4 = 4
    Button5 = 5
    Button6 = 6
    Button7 = 7

#
# lcdstatus - Keep track of LCD backlight status
#
lcdstatus = LCDStatus.OFF

DEGREE_SYMBOL = pifacecad.LCDBitmap([0x0e,0x0a,0x0e,0x00,0x00,0x00,0x00,0x00])
DEGREE_SYMBOL_INDEX = 0

#
# TempDisplay class - control LCD and buttons
#
class TempDisplay(object):
    def __init__(self, cad):
        global lcdstatus
        self.cad = cad
        lcdstatus = LCDStatus.OFF
        self.cad.lcd.backlight_off()
        self.cad.lcd.cursor_off()
        self.cad.lcd.store_custom_bitmap(DEGREE_SYMBOL_INDEX,
                                         DEGREE_SYMBOL)

    #
    # Toggle LCD back light on or off when button 0 pressed
    #
    def togglelcd(self, event=None):
        global lcdstatus
        if lcdstatus == LCDStatus.ON:
            self.cad.lcd.backlight_off()
            lcdstatus = LCDStatus.OFF
        else:
            self.cad.lcd.backlight_on()
            lcdstatus = LCDStatus.ON

    #
    # Enable Alert e-mail.  First alert e-mail disables it
    #
    def toggleAlertEmail(self, event=None):
        global alertsent
        if  alertsent == 1:
            alertsent = 0
        else:
            alertsent = 1

    #
    # Show Temperature ranges
    #
    def showTemperatureRanges(self, event=None):
        lcdlock.acquire()
        try:
            message = "Fridge : %sF-%sF" % (workLowFridge,workHiFridge)
            self.cad.lcd.clear()
            self.cad.lcd.write(message)

            message = "\nFreezer: %sF-%sF" % (workLowFreezer,workHiFreezer)
            self.cad.lcd.write(message)
        finally:
            lcdlock.release()

        time.sleep( 3 )


    #
    # Turn clear and turn off LCD when button 5 press to exit
    #
    def close(self, event=None):
        self.cad.lcd.clear()
        self.cad.lcd.backlight_off()

#
# TimerClass thread will kick off every xx seconds and measure the temperature
#
class TimerClass(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self.event = threading.Event()

    #
    # Thread run method
    #
    def run(self):
        while not self.event.is_set():
            #
            # Read and check temperature range
            #
            lcdlock.acquire()
            try:
                fridgeTemp = read_temp(1)  # sensor 1 is the Fridge unit
                freezerTemp = read_temp(2) # sensor 2 is the Freezer unit
            finally:
                lcdlock.release()

            checkTempRanges(fridgeTemp, freezerTemp)
            time.sleep(2)        # Sleep between temperature checks
            self.event.wait( 3 ) # Wait with 3 second timeout

    #
    # Thread stop
    #
    def stop(self):
        self.event.set()

#
# Non Class methods.  This are not in any class
#

#
# Signal handler to catch SIGTERM from systemd
#
def sigterm_handler(_signo, _stack_frame):
    # cleanup - clear and turn off LCD, exit
    global tempdisplay
    global lcd
    time.sleep(3)             # Give things time to settle down

    tempdisplay.close()
    lcd.clear()
    lcd.backlight_off()
    sys.exit(0)

#
# reads the actual files where the DS18B20 temp sensor records it.
#
def read_temp_raw(sensor):
    global toggledots
    semi = ((' ',':'))   # toggle colon to prove we are running

    if sensor == 1:

        if toggledots is 0:
            toggledots = 1
        else:
            toggledots = 0

        lcd.home()
        lcd.write("Fridge " + semi[toggledots] + ' ')
        f = open(device_file, 'r')
    else:
        lcd.write('\nFreezer'+ semi[toggledots] + ' ' )
        f = open(device_file_two, 'r')

    lines = f.readlines()
    f.close()
    return lines


#
# read the temperature and return the Farenheit value to caller
#
def read_temp(sensor):
    lines = read_temp_raw(sensor)
    while lines[0].strip()[-3:] != 'YES':
        time.sleep(0.1)
        lines = read_temp_raw(sensor)
    equals_pos = lines[1].find('t=')
    if equals_pos != -1:
        temp_string = lines[1][equals_pos+2:]
        temp_c = float(temp_string) / 1000.0
        temp_f = temp_c * 9.0 / 5.0 + 32.0

        lcd.write(str('{:5.2f}'.format(temp_f)))
        lcd.write_custom_bitmap(DEGREE_SYMBOL_INDEX)
        lcd.write("  ") # clear to end of line

    return str(temp_f)[:5]

#
# check the fridge and freezer temps passed in vs the allowed
# ranges and error out if invalid
#
def checkTempRanges(fridgeTemp, freezerTemp):
    global alertsent
    global updatesent
    global lastsend

    # Send update at midnight and reset alert messasge
    now = datetime.datetime.now()
    if (now.hour is 0 and now.minute is 0 and updatesent is 0):
        updatesent = 1
        alertsent = 0
        sendStatusMessage("Fridge  : " + str(fridgeTemp) + "\nFreezer: " + str(freezerTemp), emailaddress)

    # Reset updatesent to enable next update message
    if (now.hour is 0 and now.minute is 1):
        updatesent = 0

    if ((Decimal(fridgeTemp) > Decimal(workHiFridge)) or (Decimal(freezerTemp) > Decimal(workHiFreezer))):
        if alertsent is 0: # don't sent too many alert messages
            alertsent = 1
            sendAlertMessage("Temperature too warm!\n\nFridge  : " + str(fridgeTemp) + "\nFreezer: " + str(freezerTemp), emailaddress)
            lastsend = datetime.datetime.now()

    if ((Decimal(fridgeTemp) < Decimal(workLowFridge)) or (Decimal(freezerTemp) < Decimal(workLowFreezer))):
        if alertsent is 0: # don't sent too many alert messages
            alertsent = 1
            sendAlertMessage("Temperature too cold!\n\nFridge  : " + str(fridgeTemp) + "\nFreezer: " + str(freezerTemp), emailaddress)
            lastsend = datetime.datetime.now()


#
# sendAlertMessage - Send alert e-mail messasge to e-mail
#                    address in specified config file.
def sendAlertMessage(message, address):
    msg = MIMEText(message)
    msg['Subject'] = "Temperature Alert!!!"
    msg['From']    = "Temperature Monitor <" + smtplogin + ">"
    msg['To']      = address
    s = smtplib.SMTP("smtp.gmail.com", port=587) # Open server connection
    s.ehlo()            # Start conversation with SMTP server
    s.starttls()        # Server requires TLS
    s.login(smtplogin, password)  # Server requires authentication
    s.send_message(msg) # Send message
    s.quit()            # Close connection


# sendStatusMessage - Send a status e-mail messasge to e-mail
#                    address in specified config file.
def sendStatusMessage(message, address):
    msg = MIMEText(message)
    msg['Subject'] = "Temperature Status"
    msg['From']    = "Temperature Monitor <" + smtplogin + ">"
    msg['To']      = address
    s = smtplib.SMTP("smtp.gmail.com", port=587) # Open server connection
    s.ehlo()            # Start conversation with SMTP server
    s.starttls()        # Server requires TLS
    s.login(smtplogin, password)  # Server requires authentication
    s.send_message(msg) # Send message
    s.quit()            # Close connection

#
# Main Code begins here:
#

try:   #sudo authority required
  cad = pifacecad.PiFaceCAD()
except:
  print ("Error, sudo authority required")
  exit(21)

#
# listener cannot deactivate itself, so we have to wait until
# it has finished using a barrier.
#
global end_barrier
end_barrier = Barrier(2)

#
# Create lock to control write access to LCD.
#
lcdlock = threading.Lock()

#
# Instantiate TempDisplay class to control LCD
#
global tempdisplay
tempdisplay = TempDisplay(cad)

lcd = cad.lcd         # Short cut to lcd methods

#
# Setup LCD switch listener for buttons
#
switchlistener = pifacecad.SwitchEventListener(chip=cad)

#
# Button 0 will toggle LCD backlight on/off
#
switchlistener.register(LCDButtons.Button0,
                        pifacecad.IODIR_ON,
                        tempdisplay.togglelcd)

#
# Button 1 will re-enable alert e-mail
#
switchlistener.register(LCDButtons.Button1,
                        pifacecad.IODIR_ON,
                        tempdisplay.toggleAlertEmail)
#
# Button 2 show Temperature ranges
#
switchlistener.register(LCDButtons.Button2,
                        pifacecad.IODIR_ON,
                        tempdisplay.showTemperatureRanges)

#
# Button 4 will exit this application
#
switchlistener.register(LCDButtons.Button4,
                        pifacecad.IODIR_ON,
                        end_barrier.wait)

lcd.clear()           # clear LCD
lcd.backlight_off()   # Turn off backlight
lcd.home()            # Move cursor "home"
lcd.blink_off()       # Turn off blinking cursor
lcd.cursor_off()      # Turn off custor
lcd.write("Fridge Monitor\nStarting...")
time.sleep(3)

#
# modprobe adds modules to the Linux Kernel
# This grabs data from the DS18B20 Digital temperature sensors
# NOTE: need to be root to do this
#
os.system('modprobe w1-gpio')
os.system('modprobe w1-therm')

#
# The temperatur probe data will appear as a file in
# /sys/bus/wi/devices.  Each has a unique serial number
# of the format: 28-0115a51b4dff
#
base_dir = '/sys/bus/w1/devices/'
try:
  #
  # Only monitoring 2 probes.  More can be added as necessary
  #
  device_folder = glob.glob(base_dir + '28*')[0]
  device_file = device_folder + '/w1_slave'

  device_folder_two = glob.glob(base_dir + '28*')[1]
  device_file_two = device_folder_two + '/w1_slave'

except:
  print ("Error 23 reading temp sensor file, sensors connected?")
  lcd.clear()
  lcd.home()
  lcd.write("Error 23 Reading\nTemp Sensor!")
  exit(23)

# Read config file to set defaults
# Config file contains a separate line for each of these(in Farenheit)
# Low Fridge Temp
# High Fridge Temp
# Low Freezer Temp
# High Freezer Temp
# e-mail address to send alert message
# gmail account name
# gmail password
try:
  f = open('/home/pi/bin/TemperatureMonitor.cfg', 'r')
except:
  print ("Error on file Open TemperatureMonitor.cfg")
  lcd.clear()
  lcd.write("Error on open \nTemperatureMonitor.cfg")
  exit(22)
lines = f.readlines()
f.close()

#
# Setup ranges and alert e-mail address
#
workLowFridge  = int(lines[0])
workHiFridge   = int(lines[1])
workLowFreezer = int(lines[2])
workHiFreezer  = int(lines[3])
emailaddress   = lines[4].rstrip('\n')
smtplogin      = lines[5].rstrip('\n')
password       = lines[6].rstrip('\n')

#
# Register signal handler and trap SIGTERM.
#
signal.signal(signal.SIGTERM, sigterm_handler)

try:
    #
    # Create message of acceptable ranges and display
    #
    message = "Fridge:  %sF-%sF" % (workLowFridge,workHiFridge)
    lcd.clear()
    lcd.write(message)

    message = "\nFreezer: %sF-%sF" % (workLowFreezer,workHiFreezer)
    lcd.write(message)
    time.sleep( 3 )
    lcd.clear()

    #
    # Instantiate TimerClass thread.  This thread will monitor
    # the probes for updates.
    #
    tmr = TimerClass()
    tmr.start()  # start the timer thread which will wake up and measure temperature

    switchlistener.activate() # activate LCD switch listener
    end_barrier.wait()        # wait until exit

finally:
    # cleanup - shutdown listener, clear and turn off LCD, exit
    tmr.stop()                # Stop timer thread
    time.sleep(3)             # Give things time to settle down

    tempdisplay.close()
    switchlistener.deactivate()
    lcd.clear()
    lcd.backlight_off()
