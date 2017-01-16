#!/usr/bin/env python
from __future__ import print_function

#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA 02110-1301, USA.
#

__author__ = 'srodgers'
import os
import sys
import signal
from socket import socket,AF_INET, SOCK_DGRAM,SOL_SOCKET,SO_BROADCAST
from socket import error as SocketError
import logging

from threading import Thread,Event
import select
import time
import json
import argparse
import ConfigParser
import paho.mqtt.client as mqtt

# Tuneable variables
maxxplmsg = 1500
hbsleep = 120
mqtt_timeout = 60


# Program variables
connected_ev = Event()
boundport = 0
xpl_port = 0
xpl_remote_ip = ''
mqtt_port = 0



#
# Process signal to exit
#

def signal_exit(signum, stack):
    if('pidfile' in generalConfigDict and args.n is False):
        try:
            os.remove(generalConfigDict['pidfile'])
        except (OSError, IOError) as e:
            logging.error("Could not delete pidfile: {}".format(generalConfigDict['pidfile']))
    logging.info("xplmqttbridge.py shutting down")
    raise SystemExit


#
# Parse a string of xpl entities into a dictionary of dictionaries
#

def parse_xpl_entities(xplstring):

    # local function used to create a dictionary from a list of xPL key=value pairs
    def sv(l):
        d = {}
        for item in l:
            (k,v) = item.split('=')
            d[k] = v
        return d

    # Find the header and body delimiters. Let index raise a ValueError if they aren't present
    delims = ['{','}','{','}']
    offsets = []
    last = 0
    for i in range(4):
        last = xplstring.index(delims[i], last)
        offsets.append(last)

    # Extract the entities
    header = xplstring[offsets[0]+2:offsets[1]-1].replace(' ','').split('\n')
    header_dict = sv(header)
    body = xplstring[offsets[2]+2:offsets[3]-1].replace(' ','').split('\n')
    body_dict = sv(body)
    command = xplstring[0:offsets[0]].replace(' ','').replace('\n','')
    schema = xplstring[offsets[1]+1:offsets[2]].replace(' ','').replace('\n','')

    # length sanity check
    if(len(command) == 0 or len(schema) == 0 or len(header) == 0 or len(body) == 0):
        logging.warning('parse_xpl_entities: length sanity check failed')
        raise ValueError

    # Test for properly formed schema
    schema.index('.')

    # Test for properly formed command
    if(command != 'xpl-cmnd' and command != 'xpl-stat' and command != 'xpl-trig'):
        logging.warning('parse_xpl_entities: bad command')
        raise ValueError

    # Test for required entities in header
    if 'source' not in header_dict\
        or 'target' not in header_dict\
        or 'hop' not in header_dict:
        logging.warning('parse_xpl_entities: entities missing from header')
        raise ValueError

    # Put it all together
    xplentities = {'command':command,
        'header':header_dict,
        'schema':schema,
        'body':body_dict}


    return xplentities

#
# Send a heartbeat message to the hub. This runs on its own thread
#

def taskHeartBeat():

    sock = socket(AF_INET,SOCK_DGRAM)
    sock.setsockopt(SOL_SOCKET,SO_BROADCAST,1)
    xplhbmsg = "xpl-stat\n{\nhop=1\nsource=hwstar-xplmqttbridge.python\ntarget=*\n}\nhbeat.app\n{\ninterval=5\nport=" +\
               str(boundport) + "\nremote-ip=" + xpl_remote_ip +"\n}\n"
    while True:
        #print("Sending Heartbeat message")
        sock.sendto(xplhbmsg,("255.255.255.255", xpl_port))
        logging.debug("xPL heartbeat message sent")
        time.sleep(hbsleep) # Wait till it's time to send another heartbeat

# MQTT Connected callback

def on_connect(client, userdata, flags, rc):
    for entry in  mqtttoxpl:
        items = dict(Config.items(entry))
        client.subscribe(items['mqtt_sub'], qos=0)
    connected_ev.set()
    logging.info("MQTT connected")

#
# MQTT Message received callback
# MQTT to xPL path
#
def on_message(client, userdata, msg):
    message = str(msg.payload)
    topic = str(msg.topic)

    #print("Received MQTT message: {} from topic {}".format(message,topic)) # DEBUG

    for entry in mqtttoxpl:
        items = dict(Config.items(entry))
        if 'mqtt_sub' in items and topic == items['mqtt_sub'] and \
                'xpl_command' in items and\
                'xpl_source' in generalConfigDict and\
                'xpl_target' in items and\
                'xpl_schema' in items:
            # Build xPL packet string
            # Command
            xpl_out_string = items['xpl_command'] + '\n'
            # Open Header
            xpl_out_string += '{' + '\n'
            # Header Items
            xpl_out_string += 'hop=1'+ '\n'
            xpl_out_string += "source=" + generalConfigDict['xpl_source'] + '\n'
            xpl_out_string += "target=" + items['xpl_target'] + '\n'
            # Close Header
            xpl_out_string += '}' + '\n'
            # Schema
            xpl_out_string += items['xpl_schema'] + '\n'
            # Open body
            xpl_out_string += '{' + '\n'

            logging.debug('msg.payload = {}'.format(msg.payload))

            try:
                for k,v in json.loads(msg.payload).iteritems():
                    k = k.encode('ascii')
                    v = v.encode('ascii')
                    xpl_out_string += k+'='+v+'\n'
            except ValueError:
                logging.warning('on_message: format error in mqtt message')
                return
            # Close body
            xpl_out_string += '}' + '\n'


            logging.debug("Sending xPL message: {}".format(xpl_out_string))


            # Send the message to the xPL network
            sock = socket(AF_INET, SOCK_DGRAM)
            sock.setsockopt(SOL_SOCKET,SO_BROADCAST, 1)
            sock.sendto(xpl_out_string,("255.255.255.255", xpl_port))
            sock.close()

# Main program loop

def xplmqttbridge():
    global boundport

    # Initialize the xPL udp socket
    xplsock = socket(AF_INET,SOCK_DGRAM)

    # Try and bind to the base port
    try :
        boundport = xpl_port
        xpladdr = ("0.0.0.0", xpl_port)
        xplsock.bind(xpladdr)
    except SocketError:
        # A hub is running, so bind to a high port
        boundport = 50000
        while True:
            try :
                logging.info("Bound port: {}".format(boundport))
                xpladdr = ("0.0.0.0", boundport)
                xplsock.bind(xpladdr)
            except SocketError :
                #print("Except")
                boundport += 1
                continue
            break

    #print("xPL bound to port: {}".format(boundport))

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    if'mqtt_user' in generalConfigDict and 'mqtt_pass' in generalConfigDict:
        client.username_pw_set(generalConfigDict['mqtt_user'], generalConfigDict['mqtt_pass'])
    client.connect(generalConfigDict['mqtt_broker'], int(generalConfigDict['mqtt_port']), mqtt_timeout)
    client.loop_start()
    logging.info("MQTT Started")
    connected_ev.wait()

    # Start heartbeat thread
    heartbeat_th = Thread(target=taskHeartBeat)
    heartbeat_th.daemon = True
    heartbeat_th.start()

    # Wait on xPL data
    while True:


        readable, writeable, errored = select.select([xplsock],[],[],60)


        if len(readable) == 1 :
            data,addr = xplsock.recvfrom(maxxplmsg)
            #print('packet string:') # DEBUG
            #print(data) # DEBUG
            try:
                xpl_entities = parse_xpl_entities(data)
            except ValueError:
                logging.warning('xplmqttbridge: bad xPL packet')
                continue

            #print(xpl_entities) # DEBUG
            # xPL to MQTT path

            for entry in xpltomqtt:
                items = dict(Config.items(entry))

                # Apply filters which are present
                if 'xpl_source' in items:
                    if items['xpl_source'] != xpl_entities['header']['source']:
                        continue
                if 'xpl_command' in items:
                    if items['xpl_command'] != xpl_entities['command']:
                        continue
                if 'xpl_schema' in items:
                    if items['xpl_schema'] != xpl_entities['schema']:
                        continue
                if 'xpl_body_device' in items:
                    if items['xpl_body_device'] != xpl_entities['body']['device']:
                        continue

                payload = json.dumps(xpl_entities['body'])

                # If topic is defined, send a payload to it
                try:
                    mqtt_topic=items['mqtt_pub'].format(**xpl_entities['body'])
                except KeyError as e:
                    logging.warn("The parameter '{}' was not found in xpl message for mapping '{}'".format(e.args[0], entry))
                    #In case the configuration is bad we don't replace the parameters
                    mqtt_topic=items['mqtt_pub']

                logging.debug("Sending MQTT topic: {} message: {}".format(mqtt_topic, entry))
                if 'mqtt_pub' in items:
                    client.publish(mqtt_topic, payload)

#
# Main code
#
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='XPL to MQTT bridge', prog='xplmqttbridge')
    parser.add_argument('-n', action='store_true', default=False, help='Do not run in background mode')
    parser.add_argument('-c', action='store', help='Specify a configuration file')
    parser.add_argument('-d', action='store', help='Specify a logging level (1-5)', default='2')

    args = parser.parse_args()

    # Config parser init
    Config = ConfigParser.ConfigParser()


    # Read config file
    if args.c is None:
        cfgfiles = ['/etc/xplmqttbridge/xplmqttbridge.conf','./xplmqttbridge.conf']
    else:
        if os.path.isfile(args.c) is False:
            sys.exit("Config file missing: {}".format(args.c))
        cfgfiles = args.c

    Config.read(cfgfiles)

    # Get config options
    generalConfigDict = dict(Config.items("general"))

    loglevel = int(args.d)

    if(loglevel < 1):
        loglevel = 1
    elif(loglevel > 5):
        loglevel = 5

    logcode = (logging.CRITICAL, logging.ERROR, logging.WARNING, logging.INFO, logging.DEBUG)[loglevel - 1]

    if 'logfile' in generalConfigDict and args.n is False:
        logging.basicConfig(filename=generalConfigDict['logfile'], filemode='w', level=logcode)
    else:
        logging.basicConfig(level=logcode)



    xpl_port = int(generalConfigDict['xpl_port'])
    mqtt_port = int(generalConfigDict['mqtt_port'])
    xpl_remote_ip = generalConfigDict['xpl_remote_ip']

    xpltomqtt = []
    mqtttoxpl = []

    for section in Config.sections():
        if section == 'general':
            continue
        if section.startswith('xpl:'):
            xpltomqtt.append(section)
        elif section.startswith('mqtt:'):
            mqtttoxpl.append(section)

    signal.signal(signal.SIGINT, signal_exit)
    signal.signal(signal.SIGTERM, signal_exit)


    if(args.n is True):
        xplmqttbridge()

    # do the UNIX double-fork magic, see Stevens' "Advanced
    # Programming in the UNIX Environment" for details (ISBN 0201563177)
    pid = 0
    try:
        pid = os.fork()
        if pid > 0:
            # exit first parent
            sys.exit(0)
    except OSError, e:
        logging.error("fork #1 failed: {} ({})".format(e.errno, e.strerror), file=sys.stderr)
        sys.exit(1)

    # decouple from parent environment
    os.chdir("/")
    os.setsid()
    os.umask(0)

    # do second fork
    try:
        pid = os.fork()
        if pid > 0:
            # exit from second parent, print eventual PID before
            logging.info("Daemon PID: {}".format(pid))
            if 'pidfile' in generalConfigDict:
                try:
                    with open(generalConfigDict['pidfile'],'w') as pidfile:
                        pidfile.write('{}'.format(pid))
                except (OSError, IOError):
                    logging.error("Could not write pidfile: {}".format(generalConfigDict['pidfile']))
                sys.exit(0)

    except OSError, e:
        logging.error("fork #2 failed: {} ({})".format(e.errno, e.strerror), file=sys.stderr)
        sys.exit(1)

    # Ignore SIGHUP in daemon mode
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    xplmqttbridge()





