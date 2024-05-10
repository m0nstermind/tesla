import math

import keyring
from collections import deque

from dateutil import tz
from teslapy import Tesla
#import sonoff
import solaredge
import logging
import time
import datetime
from suntime import Sun, SunTimeException
import sdnotify
import solaredge_modbus
import geopy.distance
from configparser import ConfigParser

logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)

# script configuration
ini = ConfigParser()
ini.read('tesla.ini')

# minimal possible charge ampers for tesla
MIN_CHARGE_AMPS = 5
MAX_CHARGE_AMPS = 16

# 100% of energy must come from solar
SOLAR_CHARGE_RATIO = 1.0

# home GPS location
HOME_LONG = ini.getfloat('home', 'long')
HOME_LAT = ini.getfloat('home', 'latt')
HOME_COORD = (HOME_LAT, HOME_LONG)

SE_SITEID = ini.getint('solaredge', 'site_id')
SE_APIKEY = ini.get('solaredge', 'api_key')
SE_IP = ini.get('solaredge', 'modbus_ip')
SE_PORT = ini.getint('solaredge', 'modbus_port')

u = ini.get('tesla', 'user')
vehicle_id = ini.getint('tesla', 'vehicle_id')

s = solaredge.Solaredge(SE_APIKEY)
sm = solaredge_modbus.Inverter(host=SE_IP, port=SE_PORT)

tesla = Tesla(u)
sun = Sun(HOME_LAT, HOME_LONG)

def getCachedTeslaState():
    return tesla.api('VEHICLE_DATA', path_vars={"vehicle_id": vehicle_id})['response']


def getChargeState():
    return getCachedTeslaState()['charge_state']
#    tesla.endpoints['CHARGE_STATE'] = {'TYPE': 'GET', 'URI': 'api/1/vehicles/{vehicle_id}/data_request/charge_state', 'AUTH': True}
#    return tesla.api('CHARGE_STATE', path_vars={"vehicle_id": vehicle_id}, timeout=30)['response']


def checkr(r):
    if not r['response']['result']:
        logging.warning("Abnormal result: %s", r)
    return r['response']['result']


def wakeup():
    global teslaAwake
    if teslaAwake:
        return
    logging.info("...waking up tesla buses...")
    while tesla.api('WAKE_UP', {"vehicle_id": vehicle_id}, timeout=30)['response']['state'] != "online":
        logging.info("... still waking up...")
        time.sleep(5)
    time.sleep(10)
    teslaAwake = True

def teslado( name, path_vars=None, **kwargs ):
    try:
        wakeup()
        r = tesla.api(name, path_vars=path_vars, **kwargs)
        time.sleep(10)
        return checkr(r)
    except requests.exceptions.HTTPError:
        logging.error("HTTPError calling %s", name)
        return False

def setChargeLimit(limit):
    return teslado('CHANGE_CHARGE_LIMIT', {"vehicle_id": vehicle_id}, percent=limit, timeout=30)


def setChargeAmps(amps):
    return teslado('CHARGING_AMPS', {"vehicle_id": vehicle_id}, charging_amps=amps)


def startCharge():
    return teslado('START_CHARGE', {"vehicle_id": vehicle_id})


def stopCharge():
    return teslado('STOP_CHARGE', {"vehicle_id": vehicle_id}, timeout=30)

def refreshStatus():
    if cs is None or watts is None:
        return
    if isTeslaCharging:
        n.notify("STATUS={}w: Tesla is charging at {}A, battery: {}%".format(watts, charge_amps, cs['battery_level']))
    else:
        n.notify("STATUS={}w: Tesla is not charging, battery: {}%".format(watts, cs['battery_level']))


logging.info("Starting da shit")
pause = 1
isTeslaCharging = None
cs = None
v = None
ds = None
teslaAwake = False
# Inform systemd that we've finished our startup sequence...
n = sdnotify.SystemdNotifier()


# tcp modbus works over local LAN only
if sm.connect():
    defaultPause = 1 * 60
    last30watts = deque(maxlen=30)
    last30amps = deque(maxlen=30)
else:
    defaultPause = 5 * 60

# max sleep for 3 hours
maxPause = 3*3600

status=""

applied_amps = None
applied_time = time.time()
while True:
    # Sleeping between retries, invoking watchdog timer once per a minute
    pause = min( pause, maxPause )
    if pause > 30:
        last30watts.clear()
        last30amps.clear()

    if status != "":
        n.notify("STATUS=%s" % status )
  

    sleep_secs = min( pause, defaultPause )
    time.sleep(sleep_secs)
    pause -= sleep_secs
    if pause > 0:
        n.notify("STATUS=%s sleeping for %s secs more" % (status, pause) )
        n.notify("WATCHDOG=1")
        continue

    pause = defaultPause

    teslaAwake = False

    if not isTeslaCharging and (sun.get_sunrise_time() > datetime.datetime.now(tz.tzutc()) or datetime.datetime.now(tz.tzutc()) > sun.get_sunset_time()):
        logging.info("Sun is down")
        if datetime.datetime.now(tz.tzutc()) > sun.get_sunset_time():
            pause = maxPause
            status="Sun is down"
        else:
            pause = sun.get_sunrise_time() - datetime.datetime.now(tz.tzutc())
            status="Waiting for sunrise"
        continue

    try:
        if not sm.connected():
            if sm.connect():
                pause = 1
                logging.info("reconnected to modbus")
                continue

        if sm.connected():
            pause = 1
            sensors = sm.read_all()
            watts = sensors['power_ac'] * math.pow(10, sensors["power_ac_scale"])
            last30watts.append(watts)
            amps = min(sensors['l1_current'], sensors['l2_current'], sensors['l3_current']) * math.pow(10, sensors["current_scale"])
            last30amps.append(amps)
            # calculating sliding average over last 30 secs
            avg_watts = sum(last30watts) / len(last30watts)
            avg_amps = sum(last30amps) / len(last30amps)
            if len(last30watts) < 30 or len(last30amps) < 30:
                status="... collecting 30 samples: %s watts, %s A, avg: %s watts, %s A" % ( watts, amps, avg_watts, avg_amps )
                continue
            watts = round(avg_watts,2)
            amps = round(avg_amps,2)
        else:
            watts = s.get_overview(SE_SITEID)['overview']['currentPower']['power']
            amps = int(watts / 220 / 3/SOLAR_CHARGE_RATIO )

        # watts we can charge tesla with ( we reserve 1kw for home usage )
        charge_watts = int(watts - 528)
        charge_amps = int(amps - 0.8)

        if charge_amps == applied_amps and time.time() - applied_time < defaultPause:
            refreshStatus()
            continue

        applied_time = time.time()
        applied_amps = charge_amps

        logging.info("%s W %s A produced by solar, %s W %s A is available for Tesla charge, local:%s", watts, amps, charge_watts, charge_amps, sm.connected())

        v = getCachedTeslaState()
        cs = v['charge_state']
        ds = v['drive_state']

        n.notify("WATCHDOG=1")

        logging.info("... current Tesla battery level is %s%%", cs['battery_level'])

        # common rules
        if round(ds['latitude'], 3) != HOME_LAT or round(ds['longitude'], 3) != HOME_LONG:
            logging.info("Tesla is not at home (%s,%s) so charging is postponed", round(ds['latitude'], 3),
                         round(ds['longitude'], 3))
            if cs['charge_limit_soc'] == 79:
                logging.info("Setting Tesla charge limit back to 50")
                setChargeLimit(50)
            distance = geopy.distance.distance( HOME_COORD, ( ds['latitude'], ds['longitude'] ) ).km
            pause = max(distance / 50 * 3600, defaultPause)
            status = "Tesla is %s from home" % distance
            logging.info("Tesla is %s km from home (%s,%s) so charging is postponed for %s secs", distance, round(ds['latitude'], 3),
                         round(ds['longitude'], 3), pause)
            isTeslaCharging = None
            continue
        if cs['charge_limit_soc'] != 50 and cs['charge_limit_soc'] != 79:
            status="Tesla charge limit is custom (%s%%)" % cs['charge_limit_soc']
            logging.info("Tesla charge limit is custom (%s%%) so charging management is disabled", cs['charge_limit_soc'])
            isTeslaCharging = None
            if cs['battery_level'] >= 50:
                pause = defaultPause * 3
            if cs['battery_level'] >= 79:
                pause = defaultPause * 60
            continue
        if cs['battery_level'] >= 79:
            logging.info("Tesla battery is full, so charging is postponed for %s secs" % maxPause)
            if cs['charge_limit_soc'] == 79:
                logging.info("Setting Tesla charge limit back to 50")
                setChargeLimit(50)
            isTeslaCharging = False
            status="Tesla battery is full"
            pause = maxPause
            continue
        if cs['battery_level'] < 50:
            logging.info("Tesla battery is too low, forcing 16A charge")
            charge_amps = 16
            pause = defaultPause
        if cs['charge_port_latch'] != "Engaged":
            if cs['charge_port_latch'] == "<invalid>":
                wakeup()
                cs=getChargeState()
            if cs['charge_port_latch'] != "Engaged":
                logging.info("Tesla is not connected to grid: %s", cs['charge_port_latch'])
            isTeslaCharging = None
            pause = defaultPause * 5
            status="Tesla is not connected to grid"
            continue

        if isTeslaCharging is None:
            isTeslaCharging = cs['charging_state'] == "Charging"
            logging.info("Tesla charging state is %s", cs['charging_state'])

        status=""
        refreshStatus()

        # if there is enough amps for minimal charge, start it
        if charge_amps >= MIN_CHARGE_AMPS:
            charge_amps = max(charge_amps, MIN_CHARGE_AMPS)
            charge_amps = min(charge_amps, MAX_CHARGE_AMPS)
            if charge_amps != cs['charge_current_request']:
                logging.info("Setting charge current to %s", charge_amps)
                setChargeAmps(charge_amps)
                #renew charge state
                cs = getChargeState()

            logging.info("Current charge limit is %s%%", cs['charge_limit_soc'])

            if cs['charge_limit_soc'] == 50:
                logging.info("Current charge limit is %s%%, setting o 79%%", cs['charge_limit_soc'])
                if not setChargeLimit(79):
                    logging.warning("Cannot set Tesla charge limit to 79%")
                    continue
                #renew charge state
                cs = getChargeState()

            # engaging charge
            if isTeslaCharging:
                logging.info("Tesla is already charging")
                continue

            if cs['charging_state'] == "Stopped":
                logging.info("Starting charge")
                if not startCharge():
                    logging.warning("Cannot command to start charge")
                #renew charge state
                time.sleep(30)
                cs = getChargeState()

            isTeslaCharging = cs['charging_state'] == "Charging"
            if not isTeslaCharging:
                isTeslaCharging = None
            logging.info("Tesla is %s", cs['charging_state'])

        elif amps <= MIN_CHARGE_AMPS - 1:
            # shutting down charge
            if cs['charge_limit_soc'] == 79:
                logging.info("Setting Tesla charge limit back to 50")
                if not setChargeLimit(50):
                    logging.warning("Cannot set Tesla charge limit back to 50")
                    continue
                #renew charge state
                time.sleep(30)
                cs = getChargeState()

            if MAX_CHARGE_AMPS != cs['charge_current_request'] and cs['charge_current_request'] != 0:
                logging.info("Resetting charge current to %s", MAX_CHARGE_AMPS)
                setChargeAmps(charge_amps)
                #renew charge state
                cs = getChargeState()

            if not isTeslaCharging:
                logging.info("Tesla is not charging")
                continue

            if cs['charging_state'] != "Stopped" and cs['charging_state'] != "Complete":
                logging.info("Stopping charge: currently %s", cs['charging_state'])
                if not stopCharge():
                    logging.warning("Cannot stop charge")
                    continue
                #renew charge state
                time.sleep(30)
                cs = getChargeState()
            isTeslaCharging = cs['charging_state'] == "Charging"
            logging.info("Tesla is %s", cs['charging_state'])
    except Exception as e:
        applied_amps = None
        logging.error("Unexpected error: %s", e)
        continue

# {'overview': {'lastUpdateTime': '2022-08-10 17:52:12', 'lifeTimeData': {'energy': 74911.0, 'revenue': 0.89361906}, 'lastYearData': {'energy': 6210.0}, 'lastMonthData': {'energy': 6210.0}, 'lastDayData': {'energy': 6210.0}, 'currentPower': {'power': 1905.0}, 'measuredBy': 'INVERTER'}}
# call this once in 5 minutes
# print(s.get_storage_data(SE_SITEID,'2022-08-10 17:45:00','2022-08-10 18:00:00',SE_SN))
# print(s.get_overview(SE_SITEID)['overview']['currentPower']['power'])

# with teslapy.Tesla(u) as tesla:
#    v = tesla.api('CACHED_PROTO_VEHICLE_DATA', path_vars={"vehicle_id": vehicle_id})['response']
#    v = tesla.api('HONK_HORN', path_vars={"vehicle_id": vehicle_id})['response']
#    print(v)
#    vehicles = tesla.vehicle_list()
#    v = vehicles[0]
#    print(v['display_name'] +
#          ' at ' + str(vehicles[0]['charge_state']['battery_level']) + '% SoC')

#    print(v)
#    cs = v['charge_state']
#    print(cs)
#    print (tesla.endpoints)
# print(cs['charging_state']) # Stopped; null when on the go
# print(cs['charger_power']) # Stopped; null  when on the go
# print(cs['charger_voltage']) # Stopped; null  when on the go
# print(cs['battery_level']) # 64 percents
# print(cs['charge_current_request']) # 16, ampers
# print(cs['time_to_full_charge']) # 1.17 hours, decimal fraction, like 60*1.17=70 minutes
# print(cs['minutes_to_full_charge']) # 70, in minutes; 0 if full or no charge applicable
# print(cs['charge_port_latch']) # Engaged, <invalid> when on the go
# print(cs['scheduled_charging_pending']) # true; false when on the go
# print()
#    print(v.get_charge_history())
