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
#Modified by Chuck Hunley 01/11/2019
#    * Modified to publish temperatures to MQTT broker.  MQTT is used to
#      pass the temperatures to homebridge(Bridge to Apple HomeKit) .  MQTT
#      is only used if configured.
#    * Changed config file format to json
#
#Modified by Chuck Hunley 08/03/2019
#    * Create DS18B20 class for handling/reading 1-wire temperature sensors
#

#
# Import required modules
#
import json
import signal
import sys
PY3 = sys.version_info[0] >= 3
if not PY3:
    print("TemperatureMonitor only works with 'python3'.")
    sys.exit(1)

import os
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
toggledots = 1
global lastsend
lastsend = -1

global lcdlock    # Lock to control write access to LCD

# init the acceptable temperature ranges - these will be overridden by config file
rangeLowFridge  = 30
rangeHiFridge   = 45
rangeLowFreezer = 30
rangeHiFreezer  = 45

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
# DS18B20 Exception handler
#
class DS18B20Error(Exception):
    pass

class SensorNotFound(DS18B20Error):
   def __init__(self, sensorName):
        super(SensorNotFound, self).__init__('Sensor not found\n {}'.format(sensorName))


#
# DS18B20 class for reading DS18B20 1-wire temperature sensor
#
class DS18B20(object):

    DEVICES_DIR = "/sys/bus/w1/devices"
    DEVICE_FILE = "w1_slave"

    UNIT_CELSIUS     = 0x01
    UNIT_FAHRENHEIT  = 0x02

    #
    # Unit conversion functions
    #
    UNIT_CONVERSION = {
        UNIT_CELSIUS:       lambda x: x/1000,
        UNIT_FAHRENHEIT:    lambda x: x/1000 * 1.8 + 32.0 
    }

    #
    # Units 
    #
    UNIT_INDICATOR = {
        UNIT_CELSIUS:     "C", 
        UNIT_FAHRENHEIT:  "F" 
    }

    #
    # DS18B20 class constructor
    #
    def __init__(self, w1_sensor_id=None):
        self.id   = w1_sensor_id
        self.devicepath = os.path.join(self.DEVICES_DIR, self.id, self.DEVICE_FILE)

    #
    # Get unit indicator for this sensor instance
    #
    def getUnitIndicator(self):
        return self.UNIT_INDICATOR[self.unit]

    #
    # Private method to get desired conversion function
    #
    @classmethod
    def _get_temp_conversion(cls,unit=UNIT_FAHRENHEIT):
        return cls.UNIT_CONVERSION[unit]

    #
    # Read raw sensor data
    #
    def _read_temp_raw(self):
        try:
            f = open(self.devicepath, 'r')
            lines = f.readlines()
            f.close()
        except IOError:
            raise SensorNotFound(self.id)
        return lines

    #
    # Public method to get sensor temperature in desired units
    #
    def getTemperature(self, unit=UNIT_FAHRENHEIT):
        # Get raw sensor data
        data = self._read_temp_raw()

        # Save unit used for this sensor
        self.unit = unit

        # Get conversion functions for 'unit'
        convert = self._get_temp_conversion(unit)

        # Convert raw sensor data into desired temperature unit
        temperature = convert(float(data[1].split("=")[1]))

        return temperature

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
            message = "Fridge : %sF-%sF" % (rangeLowFridge,rangeHiFridge)
            self.cad.lcd.clear()
            self.cad.lcd.write(message)

            message = "\nFreezer: %sF-%sF" % (rangeLowFreezer,rangeHiFreezer)
            self.cad.lcd.write(message)
        finally:
            lcdlock.release()

        time.sleep( 3 )


    #
    # Clear and turn off LCD when button 5 press to exit
    #
    def close(self, event=None):
        self.cad.lcd.clear()
        self.cad.lcd.backlight_off()

#
# TimerClass thread will kick off every xx seconds and measure the temperature
#
class TimerClass(threading.Thread):
    def __init__(self, fridge, freezer):
        threading.Thread.__init__(self)
        self.event   = threading.Event()
        self.fridge  = fridge
        self.freezer = freezer

    #
    # Thread run method
    #
    def run(self):
        global toggledots

        #
        # Loop until internal flag is set to true.
        #
        while not self.event.is_set():
            #
            # Read and check temperature range
            #
            lcdlock.acquire()
            try:

                semi = ((' ',':'))   # toggle colon to prove we are running

                if toggledots is 0:
                    toggledots = 1
                else:
                    toggledots = 0

                lcd.home()

                lcd.write("Fridge " + semi[toggledots] + ' ')
                try:    
                    fridgeTemp  = self.fridge.getTemperature()
                    lcd.write(str('{:5.2f}'.format(fridgeTemp)))
                    lcd.write_custom_bitmap(DEGREE_SYMBOL_INDEX)
                except SensorNotFound:
                    lcd.write("------")

                lcd.write("  ") # clear to end of line

                lcd.write('\nFreezer'+ semi[toggledots] + ' ' )
                try:
                    freezerTemp = self.freezer.getTemperature()
                    lcd.write(str('{:5.2f}'.format(freezerTemp)))
                    lcd.write_custom_bitmap(DEGREE_SYMBOL_INDEX)
                except SensorNotFound:
                    lcd.write("------")
                lcd.write("  ") # clear to end of line
            finally:
                lcdlock.release()

            checkTempRanges(fridgeTemp, freezerTemp)

            #
            # Check if time to publish to MQTT
            #

            if useMQTT:
                #
                # List to hold MQTT messages
                #
                mqtt_messages = []

                #
                # Homebridge only accepts temperatures in celsius.  Convert
                # to Celsius.  HomeKit will convert to Fahrenheit 
                #
                temperature = float(fridgeTemp)
                if fridge.unit is fridge.UNIT_FAHRENHEIT:
                    temperature = (temperature - 32) * 5/9
                mqtt_messages.append({'topic' : fridgeTopic, 'payload' : str('{:5.2f}'.format(temperature)),})

                temperature = float(freezerTemp)
                if freezer.unit is freezer.UNIT_FAHRENHEIT:
                    temperature = (temperature - 32) * 5/9
                mqtt_messages.append({'topic' : freezerTopic,'payload' : str('{:5.2f}'.format(temperature)),})
                publish.multiple(mqtt_messages, hostname=mqtthostname)

            time.sleep(2)        # Sleep between temperature checks
            self.event.wait( 3 ) # Wait with 3 second timeout

    #
    # Thread stop
    #
    def stop(self):
        self.event.set()

#
# Non Class methods.  These are not in any class
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
        sendStatusMessage("Fridge  : " + str('{:5.2f}'.format(fridgeTemp)) + "\nFreezer: " + str('{:5.2f}'.format(freezerTemp)) + "\n--\n", emailaddress)

    # Reset updatesent to enable next update message
    if (now.hour is 0 and now.minute is 1):
        updatesent = 0

    if ((Decimal(fridgeTemp) > Decimal(rangeHiFridge)) or (Decimal(freezerTemp) > Decimal(rangeHiFreezer))):
        if alertsent is 0: # don't sent too many alert messages
            alertsent = 1
            sendAlertMessage("Temperature too warm!\n\nFridge  : " + str('{:5.2f}'.format(fridgeTemp)) + "\nFreezer: " + str('{:5.2f}'.format(freezerTemp)) + "\n--\n", emailaddress)
            lastsend = datetime.datetime.now()

    if ((Decimal(fridgeTemp) < Decimal(rangeLowFridge)) or (Decimal(freezerTemp) < Decimal(rangeLowFreezer))):
        if alertsent is 0: # don't sent too many alert messages
            alertsent = 1
            sendAlertMessage("Temperature too cold!\n\nFridge  : " + str('{:5.2f}'.format(fridgeTemp)) + "\nFreezer: " + str('{:5.2f}'.format(freezerTemp)) + "\n--\n", emailaddress)
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

#
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

try:   # sudo authority required
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
# Read config file to set defaults
#
with open(os.path.join(sys.path[0], 'TemperatureMonitor.json'), 'r') as f:
    config = json.load(f)

#
# Setup sensors, ranges, alert e-mail address, and MQTT configuration
#
try:
    freezerSensor = config['SENSORS']['Freezer']
except:
    lcd.write("No Freezer\nSensor in config")
    tempdisplay.close()
    lcd.clear()
    lcd.backlight_off()
    exit(28)

try:
    fridgeSensor = config['SENSORS']['Refrigerator']
except:
    lcd.write("No Fridge\nSensor in config")
    tempdisplay.close()
    lcd.clear()
    lcd.backlight_off()
    exit(29)

try:
    rangeLowFridge  = config['RANGES']['LowFridge']
except:
    pass  # if LowFridge not in config.json, use default

try:
    rangeHiFridge   = config['RANGES']['HighFridge']
except:
    pass # if HighFridge not in config.json, use default

try:
    rangeLowFreezer = config['RANGES']['LowFreezer']
except:
    pass # if LowFreezer not in config.son, use default

try:
    rangeHiFreezer  = config['RANGES']['HighFreezer']
except:
    pass # if HighFreezer not in config.json, use default

#
# Just in case message needs to be displayed to LCD
#
lcd.clear()
lcd.home()

try:
    emailaddress = config['ALERTEMAIL']['EmailAddress']
except:
    lcd.write("No EmailAddress\nin config")
    time.sleep(5)
    tempdisplay.close()
    lcd.clear()
    lcd.backlight_off()
    exit(25)

try:
    smtplogin = config['ALERTEMAIL']['GmailAccount']
except:
    lcd.write("No GmailAccount\nin config")
    tempdisplay.close()
    lcd.clear()
    lcd.backlight_off()
    exit(26)

try:
    password = config['ALERTEMAIL']['GmailPassword']
except:
    lcd.write("No GmailPassword\nin config")
    tempdisplay.close()
    lcd.clear()
    lcd.backlight_off()
    exit(27)

#
# MQTT
# If no mqtthostname, the consider MQTT disabled.
# if there is a mqtt hostname, then there must be a fridgeTopic and freezerTopic
#
try:
    mqtthostname = config['MQTT']['HOSTNAME']
except:
    mqtthostname = None
    lcd.clear()
    lcd.home()
    lcd.write("MQTT Not Enabled")
    time.sleep(5)

if mqtthostname:
    try:
        #
        # Import MQTT to publish temperatures to MQTT broker
        #
        import paho.mqtt.client as mqtt
        import paho.mqtt.publish as publish
        mqttinstalled = True
    except:
        lcd.clear()
        lcd.home()
        lcd.write("Paho MQTT\nNot Installed")
        time.sleep(5)
        mqttinstalled = False

    if mqttinstalled:
        try:
            fridgeTopic    = config['MQTT']['FRIDGE_TOPIC']
            freezerTopic   = config['MQTT']['FREEZER_TOPIC']
            useMQTT        = True
        except:
            lcd.clear()
            lcd.home()
            lcd.write("Error 24 MQTT\nTopic Config")
            tempdisplay.close()
            lcd.clear()
            lcd.backlight_off()
            exit(24)
    else:
        useMQTT = False

#
# Register signal handler and trap SIGTERM.
#
signal.signal(signal.SIGTERM, sigterm_handler)

try:
    #
    # Create message of acceptable ranges and display
    #
    message = "Fridge:  %sF-%sF" % (rangeLowFridge,rangeHiFridge)
    lcd.clear()
    lcd.write(message)

    message = "\nFreezer: %sF-%sF" % (rangeLowFreezer,rangeHiFreezer)
    lcd.write(message)
    time.sleep( 3 )
    lcd.clear()

    #
    # Create instance for each sensor to monitor
    #
    try:
        fridge  = DS18B20(freezerSensor)
        fridge.getTemperature()
    except SensorNotFound as e:
        lcd.write(e)
        time.sleep( 3 )

    try:
        freezer = DS18B20(fridgeSensor)
        freezer.getTemperature()
    except SensorNotFound as e:
        lcd.write(e)
        time.sleep( 3 )

    #
    # Instantiate TimerClass thread.  This thread will monitor
    # the probes for updates.
    #
    tmr = TimerClass(fridge, freezer)
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
