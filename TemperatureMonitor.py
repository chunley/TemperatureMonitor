#!/usr/bin/env python3
#
# TemperatureMonitory.py
#   Raspberrypi based refrigerator and freezer temperature monitor.
#
# Based on Adafruit's Raspberry Pi Lesson 11 Temperature sensing tutorial
# by Simon Monk
#    http://tinyurl.com/on5tpdr
# Modified by Tim Massaro 2/2014
#    http://tinyurl.com/zsntd48
#    This script now uses a Raspberry Pi, Adafruit PiPlate LCD and
#    two DS18B20 temp sensor to monitor the freezer and fridge unit
#    at Channel One Food Shelf
# Modified by Chuck Hunley 10/9/2016
#    Modified to use PiFace Control and Display 2.  The buttons are
#    event driven instead of polling like Adafruit PiPlate LCD
#    Modified to send an e-mail alert via smtp.gmail.com instead of text msg.
#
# Modified by Chuck Hunley 01/11/2019
#    * Modified to publish temperatures to MQTT broker.  MQTT is used to
#      pass the temperatures to homebridge(Bridge to Apple HomeKit) .  MQTT
#      is only used if configured.
#    * Changed config file format to json
#
# Modified by Chuck Hunley 08/03/2019
#    * Create DS18B20 class for handling/reading 1-wire temperature sensors
#
# Modified by Chuck Hunley 08/15/2019
#    * Add support for Celsius and make desired temperature units configurable
#
# Modified by Chuck Hunley 08/28/2019
#    * Make status update e-mail time sent configurable
#    * Make interval between alert e-mails configurable
#
# Modified by Chuck Hunley 10/07/2019
#    * Remove hack for mobile Outlook not displaying last line of email
#    * Catch and pass exception from publish.multiple().
#
# Modified by Chuck Hunley 03/14/2021
#    * Add Adafruit IO integration


#
# Import required modules
#
import os
import json
import signal
import sys
PY3 = sys.version_info[0] >= 3
if not PY3:
    print("TemperatureMonitor only works with 'python3'.")
    sys.exit(1)

import time
import datetime

import smtplib
import email
from email.mime.text import MIMEText

try:
    from StringIO import StringIO  # pylint: disable=import-error
except ImportError:
    from io import StringIO        # pylint: disable=import-error

#
# Import Adafruit IO library
#
try:
    from Adafruit_IO import Client  # pylint: disable=import-error
    print("Adafruit IO installed.")
    adafruitIO_installed = True
except ImportError:
    print("Adafruit IO not installed.")
    adafruitIO_installed = None
    Client = None

#
# Import time and sleep module
#
from time import sleep

#
# Import threading module
#
import threading
from threading import Barrier

#
# Import PiFace CaD modules
#
import pifacecommon  # pylint: disable=import-error
import pifacecad     # pylint: disable=import-error

from enum import Enum
from enum import IntEnum
from decimal import Decimal

global alert_enabled     # used to avoid sending too many alert emails
global toggle_dots       # toggle the colon on LCD to indicate running
global update_enabled    # Daily update sent
global last_update       # Time of last status update message
global last_alert        # Time of last alert message

alert_enabled  = 1       # Initialize to send.  Button 1 disables
update_enabled = 1
toggle_dots    = 1
last_update    = -1
last_alert     = -1

# HH:MM when to send status e-mail
global status_report_time
status_report_time = {}

# HH interval betweenm sending alert e-mail
global alert_interval
alert_interval = 1

global lcdlock    # Lock to control write access to LCD

# Initialize the acceptable temperature ranges - these will be overridden
# by config file
rangeLowFridge  = 30
rangeHiFridge   = 45
rangeLowFreezer = 30
rangeHiFreezer  = 45

# Unit identier for displaying Temperature ranges
cUnit           = "F"

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
class DS18B20:

    DEVICES_DIR = "/sys/bus/w1/devices"
    DEVICE_FILE = "w1_slave"

    UNIT_CELSIUS     = 0x01
    UNIT_FAHRENHEIT  = 0x02

    UNIT_NAME_CELSIUS    = "Celsius"
    UNIT_NAME_FAHRENHEIT = "Fahrenheit"

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
    # Unit selector given unit string name
    #
    UNIT_SELECTOR = {
        UNIT_NAME_CELSIUS    : UNIT_CELSIUS,
        UNIT_NAME_FAHRENHEIT : UNIT_FAHRENHEIT
    }

    #
    # DS18B20 class constructor
    #
    def __init__(self, w1_sensor_id=None, unit=UNIT_FAHRENHEIT):
        self.id   = w1_sensor_id
        self.devicepath = os.path.join(self.DEVICES_DIR, self.id, self.DEVICE_FILE)
        self.unit = unit

    #
    # Get unit indicator for this sensor instance
    #
    def getUnitIndicator(self):
        return self.UNIT_INDICATOR[self.unit]

    #
    # Select units
    #
    @classmethod
    def getUnit(cls, strUnit=UNIT_NAME_FAHRENHEIT):
        return cls.UNIT_SELECTOR[strUnit]

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
            with open(self.devicepath, 'r') as f:
                lines = f.readlines()
        except IOError:
            raise SensorNotFound(self.id)
        return lines

    #
    # Public method to get sensor temperature in desired units
    #
    def getTemperature(self, unit=None):
        # Get raw sensor data
        data = self._read_temp_raw()

        #
        # If unit is None, then use default when class was instantiated,
        # otherwise user passed in unit as override.
        #
        if unit is None:
            unit = self.unit

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
    # Button callback
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
    # Enable Alert e-mail.
    # Button callback
    #
    def toggleAlertEmail(self, event=None):
        global alert_enabled
        global update_enabled
        if  alert_enabled:
            alert_enabled = 0
        else:
            alert_enabled = 1

        if update_enabled:
            update_enabled = 0
        else:
            update_enabled = 1

    #
    # Show Temperature ranges
    # Button callback
    #
    def showTemperatureRanges(self, event=None):
        lcdlock.acquire()
        try:
            temprange = "{0:>3}{2}{1:>3}{2}".format(rangeLowFridge, rangeHiFridge, cUnit)
            message   = "Fridge :{0:>8}".format(temprange)
            self.cad.lcd.clear()
            self.cad.lcd.write(message)

            temprange = "{0:>3}{2}{1:>3}{2}".format(rangeLowFreezer, rangeHiFreezer, cUnit)
            message   = "\nFreezer:{0:>8}".format(temprange)
            self.cad.lcd.write(message)
        finally:
            lcdlock.release()

        time.sleep( 3 )


    #
    # Clear and turn off LCD when button 5 pressed to exit
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
        global toggle_dots

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

                if toggle_dots == 0:
                    toggle_dots = 1
                else:
                    toggle_dots = 0

                lcd.home()

                try:
                    fridgeTemp  = self.fridge.getTemperature()
                    lcd.write("Fridge " + semi[toggle_dots] + ' ')
                    lcd.write(str('{:6.2f}'.format(fridgeTemp)))
                    lcd.write_custom_bitmap(DEGREE_SYMBOL_INDEX)
                except SensorNotFound:
                    fridgeTemp = -999
                    lcd.write("------")

                lcd.write("  ") # clear to end of line

                try:
                    freezerTemp = self.freezer.getTemperature()
                    lcd.write('\nFreezer'+ semi[toggle_dots] + ' ' )
                    lcd.write(str('{:6.2f}'.format(freezerTemp)))
                    lcd.write_custom_bitmap(DEGREE_SYMBOL_INDEX)
                except SensorNotFound:
                    lcd.write("------")
                    freezerTemp = -999
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

                #
                # If in publish.multiple and a signal is sent to shutdown,
                # or any other exception occures it takes down this thread
                # Just 'pass' it for now.  The next call to publish a message
                # should try to re-connect to the MQTT broker.
                #
                try:
                    publish.multiple(mqtt_messages, hostname=mqtthostname)
                except:
                    print("MQTT publish failed.")
                    pass

            #
            # Check iF Adafruit IO is enabled and publish
            # If the publish fails, just allow it and continue.
            #
            if useAdafruitIO:
                try:
                    aio.send_data(fridge_feed.key, float(fridgeTemp))
                    aio.send_data(freezer_feed.key, float(freezerTemp))
                except:
                    print("Adafruit IO publish failed.")
                    pass

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
    global alert_enabled
    global update_enabled
    global last_update
    global last_alert
    global alert_interval
    global status_report_time

    # Send update at midnight and reset alert messasge
    now = datetime.datetime.now()

    #
    # Only send update if enabled
    #
    if update_enabled:
        # Initialize last_update if first pass
        if last_update == -1:
            last_update = now

        #
        # Send update at HH and MM, midnight is the default
        # Determine elapsed time since last update to avoid
        # over sending update.
        #
        elapsed = now - last_update

        if (now.hour is status_report_time['HH'] and now.minute is status_report_time['MM'] and elapsed > datetime.timedelta(minutes=2)):
            sendStatusMessage("Fridge  : " + str('{:5.2f}'.format(fridgeTemp)) + "\nFreezer: " + str('{:5.2f}'.format(freezerTemp)), emailaddress)
            last_update = datetime.datetime.now()

    #
    # Only send alert e-mail if enabled.
    #
    if alert_enabled:
        # Initialize last_alert if first pass
        if last_alert == -1:
            last_alert = now

        elapsed = now - last_alert

        if ((Decimal(fridgeTemp) > Decimal(rangeHiFridge)) or (Decimal(freezerTemp) > Decimal(rangeHiFreezer))):
            if elapsed > datetime.timedelta(hours=alert_interval): # don't sent too many alert messages
                sendAlertMessage("Temperature too warm!\n\nFridge  : " + str('{:5.2f}'.format(fridgeTemp)) + "\nFreezer: " + str('{:5.2f}'.format(freezerTemp)), emailaddress)
                last_alert = datetime.datetime.now()

        if ((Decimal(fridgeTemp) < Decimal(rangeLowFridge)) or (Decimal(freezerTemp) < Decimal(rangeLowFreezer))):
            if elapsed > datetime.timedelta(hours=alert_interval): # don't send too many alert messages
                sendAlertMessage("Temperature too cold!\n\nFridge  : " + str('{:5.2f}'.format(fridgeTemp)) + "\nFreezer: " + str('{:5.2f}'.format(freezerTemp)), emailaddress)
                last_alert = datetime.datetime.now()

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
# Just in case message needs to be displayed to LCD
#
lcd.clear()
lcd.home()

#
# Read config file to set defaults
#
try:
    with open(os.path.join(sys.path[0], 'TemperatureMonitor.json'), 'r') as f:
        config = json.load(f)
except IOError as error:
    lcd.write("Config File\nNot found.")
    time.sleep(5)
    tempdisplay.close()
    lcd.clear()
    lcd.backlight_off()
    exit(30)

#
# Setup sensors, units, time to report status, ranges, alert e-mail address, and MQTT configuration
#
try:
    freezerSensor = config['SENSORS']['Freezer']
except:
    lcd.write("No Freezer\nSensor in config")
    time.sleep(5)
    tempdisplay.close()
    lcd.clear()
    lcd.backlight_off()
    exit(28)

try:
    fridgeSensor = config['SENSORS']['Refrigerator']
except:
    lcd.write("No Fridge\nSensor in config")
    time.sleep(5)
    tempdisplay.close()
    lcd.clear()
    lcd.backlight_off()
    exit(29)

try:
    strUnit = config['UNIT']
except:
    strUnit = "Fahrenheit"
    pass

unit = DS18B20.getUnit(strUnit)

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
    time.sleep(5)
    tempdisplay.close()
    lcd.clear()
    lcd.backlight_off()
    exit(26)

try:
    password = config['ALERTEMAIL']['GmailPassword']
except:
    lcd.write("No GmailPassword\nin config")
    time.sleep(5)
    tempdisplay.close()
    lcd.clear()
    lcd.backlight_off()
    exit(27)

try:
    strStatusTime = config['ALERTEMAIL']['STATUS_TIME']
    hour, minute = strStatusTime.split(":")
    if int(hour) not in range(0,25):
        status_report_time['HH'] = 0
    else:
        status_report_time['HH'] = int(hour)

    if int(minute) not in range(0,60):
        status_report_time['MM'] = 0
    else:
        status_report_time['MM'] = int(minute)
except:
    # If no STATUS, default to midnight
    status_report_time['HH'] = 0
    status_report_time['MM'] = 0

try:
    strAlertInterval = config['ALERTEMAIL']['ALERT_INTERVAL']
    alert_interval = int(strAlertInterval)
    if alert_interval not in range(1,25):
        alert_interval = 1
except:
    # No or invalid alert interval, default to 1 hr
    alert_interval = 1

#
# MQTT
# If no mqtthostname, the consider MQTT disabled.
# if there is a mqtt hostname, then there must be a fridgeTopic and freezerTopic
#
try:
    mqtthostname = config['MQTT']['BROKER_HOSTNAME']
    print('mqtthostname: >{0}<'.format(mqtthostname))
except:
    mqtthostname = None
    lcd.clear()
    lcd.home()
    lcd.write("MQTT not enabled")
    print("MQTT not enabled")
    time.sleep(5)

if mqtthostname:
    try:
        #
        # Import MQTT to publish temperatures to MQTT broker
        #
        import paho.mqtt.client as mqtt         # pylint: disable=import-error
        import paho.mqtt.publish as publish     # pylint: disable=import-error
        mqttinstalled = True
        print("Paho MQTT installed.")
    except:
        lcd.clear()
        lcd.home()
        lcd.write("Paho MQTT\nNot Installed")
        print("Paho MQTT not installed.")
        time.sleep(5)
        mqttinstalled = False

    if mqttinstalled:
        try:
            fridgeTopic    = config['MQTT']['FRIDGE_TOPIC']
            print('topic: {0}'.format(fridgeTopic))
            freezerTopic   = config['MQTT']['FREEZER_TOPIC']
            print('topic: {0}'.format(freezerTopic))
            useMQTT        = True
        except:
            lcd.clear()
            lcd.home()
            lcd.write("Error 24 MQTT\nTopic Config")
            time.sleep(5)
            tempdisplay.close()
            lcd.clear()
            lcd.backlight_off()
            exit(24)
    else:
        useMQTT = False
        print("MQTT not installed.")

#
# Get Adafruit IO configuration.  If no, ADAFRUITIO, then
# consider Adafruit IO disabled.
#
try:
    adafruit_username = config['ADAFRUITIO']['UserName']
    adafruit_key      = config['ADAFRUITIO']['Key']
    fridge_key        = config['ADAFRUITIO']['RefrigeratorKey']
    freezer_key       = config['ADAFRUITIO']['FreezerKey']
    useAdafruitIO     = True
except:
    lcd.clear()
    lcd.home()
    lcd.write("Adafruit ID\nNot Enabled")
    time.sleep(5)
    useAdafruitIO = False
    lcd.clear()
    adafruit_username = None
    adafruit_key = None
    fridge_key = None
    freezer_key = None

#
# Setup Adafruit IO connection and feeds
#
if useAdafruitIO and adafruitIO_installed is not None:
    try:
        aio          = Client(adafruit_username, adafruit_key)
        fridge_feed  = aio.feeds(fridge_key)
        freezer_feed = aio.feeds(freezer_key)
    except:
        lcd.clear()
        lcd.home()
        lcd.write("Adafruit IO\nFailed")
        print("Adafruit IO failed.")
        time.sleep(5)
        tempdisplay.close()
        lcd.clear()
        lcd.backlight_off()
        exit(26)


# Register signal handler and trap SIGTERM.
#
signal.signal(signal.SIGTERM, sigterm_handler)

try:
    #
    # Create instance for each sensor to monitor
    #
    try:
        fridge  = DS18B20(fridgeSensor, unit)
        fridge.getTemperature()
    except SensorNotFound as e:
        lcd.write(e)
        time.sleep( 3 )
        tempdisplay.close()
        lcd.clear()
        lcd.backlight_off()
        exit(27)

    try:
        freezer = DS18B20(freezerSensor, unit)
        freezer.getTemperature()
    except SensorNotFound as e:
        lcd.write(e)
        time.sleep( 3 )
        tempdisplay.close()
        lcd.clear()
        lcd.backlight_off()
        exit(28)

    #
    # Get unit indicator "C" or "F" for display
    #
    cUnit = freezer.getUnitIndicator()

    #
    # Create message of acceptable ranges and display
    #
    tempdisplay.showTemperatureRanges()

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
