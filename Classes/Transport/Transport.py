# !/usr/bin/env python3
# coding: utf-8 -*-
#
# Author: zaraki673 & pipiche38
#

import threading
import Domoticz
import socket
import serial
import queue
import time

import json

from threading import Semaphore
from queue import PriorityQueue, Queue

from Classes.Transport.sqnMgmt import sqn_init_stack, sqn_generate_new_internal_sqn
from Classes.Transport.readerThread import open_zigate_and_start_reader, shutdown_reader_thread
from Classes.Transport.writerThread import start_writer_thread
from Classes.Transport.forwarderThread import start_forwarder_thread
from Classes.Transport.tools import (
    initialize_command_protocol_parameters,
    waiting_for_end_thread,
    stop_waiting_on_queues,
)
from Classes.Transport.readDecoder import decode_and_split_message

from Modules.zigateConsts import MAX_SIMULTANEOUS_ZIGATE_COMMANDS


class ZigateTransport(object):
    def __init__(
        self,
        hardwareid,
        DomoticzBuild,
        DomoticzMajor,
        DomoticzMinor,
        transport,
        statistics,
        pluginconf,
        F_out,
        log,
        serialPort=None,
        wifiAddress=None,
        wifiPort=None,
    ):
        # Call back function to send back to plugin
        self.F_out = F_out  # Function to call to bring the decoded Frame at plugin

        # Logging
        self.log = log
        self.previousExtendedErrorCode = ""
        self.previousEEC_time = 0

        # Line management for 0x8001 message
        self.newline_required = True

        # Statistics
        self.statistics = statistics
        self.pluginconf = pluginconf

        # Communication/Transport link attributes
        self.hardwareid = hardwareid
        self._connection = None  # connection handle
        self._ReqRcv = bytearray()  # on going receive buffer
        self._last_raw_message = bytearray()
        self._transp = None  # Transport mode USB or Wifi
        self._serialPort = None  # serial port in case of USB
        self._wifiAddress = None  # ip address in case of Wifi
        self._wifiPort = None  # wifi port
        self._connection_break = False

        # Monitoring ZiGate PDUs
        self.apdu = None
        self.npdu = None

        # Semaphore to manage when to send a commande to ZiGate
        self.semaphore_gate = Semaphore(value=MAX_SIMULTANEOUS_ZIGATE_COMMANDS)

        # Running flag for Thread. Switch to False to stop the Threads
        self.running = True

        # Management of Commands sent to ZiGate
        self.ListOfCommands = {}

        # Last NwkId with a ACk failure
        self.last_nwkid_failure = None

        # Writer

        self.writer_list_in_queue = []
        self.writer_queue = PriorityQueue()
        self.writer_thread = None
        self.prioriy_sqn = 0
        self.tcp_send_queue = Queue()  # We use a Queue as socket is not thread-safe in python

        # Reader
        self.reader_thread = None

        # Forwarder

        self.forwarder_queue = Queue()
        self.forwarder_thread = None

        # Firmware Management
        self.FirmwareVersion = None
        self.FirmwareMajorVersion = None
        self.ZiGateHWVersion = None

        self.firmware_compatibility_mode = False  # 31a and below
        self.firmware_with_aps_sqn = False  # Available from 31d
        self.firmware_with_8012 = False  # Available from 31e
        self.firmware_nosqn = False
        self.PDMCommandOnly = False

        # DomoticzVersion: 2020.2 (build 12741) is minimum Dz version where full multithread is possible
        self.force_dz_communication = False

        # Initialise SQN Management
        sqn_init_stack(self)

        # Initialise Command Protocol Parameters
        initialize_command_protocol_parameters()

        if transport in ("USB", "DIN", "V2-USB", "V2-DIN", "PI", "V2-PI"):
            self._transp = transport
            self._serialPort = serialPort

        elif str(transport) in ("Wifi", "V2-Wifi"):
            self._transp = transport
            self._wifiAddress = wifiAddress
            self._wifiPort = wifiPort

        else:
            Domoticz.Error("Unknown Transport Mode: >%s<" % transport)
            self._transp = "None"

    # for Statistics usage
    def get_forwarder_queue(self):
        return self.forwarder_queue.qsize()

    def get_writer_queue(self):
        return self.forwarder_queue.qsize()

    def update_ZiGate_HW_Version(self, version):
        self.ZiGateHWVersion = version

    def update_ZiGate_Version(self, FirmwareVersion, FirmwareMajorVersion):
        self.FirmwareVersion = FirmwareVersion
        self.FirmwareMajorVersion = FirmwareMajorVersion

    def thread_transport_shutdown(self):
        self.running = False

    def pdm_lock_status(self):
        return self.PDMCommandOnly

    def loadTransmit(self):
        # Provide the Load of the Sending Queue
        return self.writer_queue.qsize()

    def sendData(self, cmd, datas, highpriority=False, ackIsDisabled=False, waitForResponseIn=False, NwkId=None):
        # We receive a send Message command from above ( plugin ),
        # send it to the sending queue

        if (cmd, datas) in self.writer_list_in_queue:
            if self.pluginconf.pluginConf["debugzigateCmd"]:
                self.logging_send(
                    "Log", "sendData - Warning %s/%s already in queue this command is dropped" % (cmd, datas)
                )
            return None
        self.writer_list_in_queue.append((cmd, datas))

        InternalSqn = sqn_generate_new_internal_sqn(self)
        message = {
            "cmd": cmd,
            "datas": datas,
            "ackIsDisabled": ackIsDisabled,
            "waitForResponseIn": waitForResponseIn,
            "InternalSqn": InternalSqn,
            "NwkId": NwkId,
            "TimeStamp": time.time(),
        }
        try:
            if self.writer_queue.qsize() == 0:
                # Queue is empty, we reset the priority_sqn number to 0
                self.prioriy_sqn = 0
            if highpriority:
                self.logging_send(
                    "Debug",
                    "Hih Priority command Hsqn: %s Cmd: %s Data: %s i_sqn: %s"
                    % (self.prioriy_sqn, message["cmd"], message["datas"], message["InternalSqn"]),
                )
                self.writer_queue.put((self.prioriy_sqn, str(json.dumps(message))))
                self.prioriy_sqn += 1
            else:
                self.writer_queue.put((InternalSqn, str(json.dumps(message))))

        except queue.Full:
            self.logging_send("Error", "sendData - writer_queue Full")

        except Exception as e:
            self.logging_send("Error", "sendData - Error: %s" % e)

        return InternalSqn

    def on_message(self, data):
        # Message sent via Domoticz .
        decode_and_split_message(self, data)

    # Transport / Opening / Closing Communication
    def set_connection(self):
        if self._connection:
            del self._connection
            self._connection = None
        open_connection(self)

    def open_zigate_connection(self):
        if not self._connection:
            self.set_connection()
        if (not self.pluginconf.pluginConf["byPassDzConnection"] or self.force_dz_communication) and self._connection:
            self._connection.Connect()
        self.logging_send("Log", "Connection open: %s" % self._connection)

    def re_connect_zigate(self):

        self.logging_send("Error", "Reconnection: Old: %s" % self._connection)
        if self.pluginconf.pluginConf["byPassDzConnection"] and not self.force_dz_communication:
            if self._connection:
                self._connection.close()
                self._connection = None
                time.sleep(1.0)
        else:
            self.logging_send("Error", "---> Connection state: %s" % self._connection.Connected())
            if self._connection.Connected():
                self.logging_send("Error", "--->Connection still exist !!! Need to shutdown")
                self.close_zigate_connection()
                self._connection = None

        self.logging_send("Error", "--->Connection still exist !!! Need to shutdown")
        self.open_zigate_connection()

    def close_zigate_connection(self):
        self.logging_send("Log", "Request closing connection: %s" % self._connection)

        self.running = False  # It will shutdown the Thread

        if self.pluginconf.pluginConf["byPassDzConnection"] and not self.force_dz_communication:
            self.logging_send("Log", "-- shutdown reader thread")
            shutdown_reader_thread(self)

            time.sleep(1.0)
            self.logging_send("Log", "-- waiting for end of thread")
            waiting_for_end_thread(self)
            self.logging_send("Log", "-- thread endeed")

        else:
            stop_waiting_on_queues(self)
            waiting_for_end_thread(self)
            self._connection.Disconnect()

        self._connection = None

    # Login mecanism
    def logging_send(self, logType, message, NwkId=None, _context=None):
        # Log all activties towards ZiGate
        self.log.logging("TransportTx", logType, message, context=_context)

    def logging_forwarded(self, logType, message, NwkId=None, _context=None):
        self.log.logging("TransportFrwder", logType, message, context=_context)

    def logging_receive(self, logType, message, nwkid=None, _context=None):
        # Log all activities received from ZiGate
        self.log.logging("TransportRx", logType, message, nwkid=nwkid, context=_context)

    def transport_error_context(self, context):
        if context is None:
            context = {}
        context["Queues"] = {
            "ListOfCommands": dict.copy(self.ListOfCommands),
            "writeQueue": str(self.writer_queue.queue),
            "forwardQueue": str(self.forwarder_queue.queue),
            "SemaphoreValue": self.semaphore_gate._value,
            "ForwardedQueueCurrentSize": self.get_forwarder_queue(),
            "WriterQueueCurrentSize": self.get_writer_queue(),
        }
        context["Firmware"] = {
            "writerTimeOut": self.pluginconf.pluginConf["writerTimeOut"],
            "compatibility_mode": self.firmware_compatibility_mode,
            "dzCommunication": self.force_dz_communication,
            "with_aps_sqn": self.firmware_with_aps_sqn,
            "with_8012": self.firmware_with_8012,
            "FirmwareWithNoSQN": self.firmware_nosqn,
            "nPDU": self.npdu,
            "aPDU": self.apdu,
        }

        if "TransportErrorLevel" in self.pluginconf.pluginConf and self.pluginconf.pluginConf["TransportErrorLevel"]:
            context["Sqn Management"] = {
                "sqn_ZCL": self.sqn_zcl,
                "sqn_ZDP": self.sqn_zdp,
                "sqn_APS": self.sqn_aps,
                "current_SQN": self.current_sqn,
            }
            context["inMessage"] = {
                "ReqRcv": str(self._ReqRcv),
            }
        context["Thread"] = {
            "byPassDzCommunication": self.pluginconf.pluginConf["byPassDzConnection"],
            "ThreadName": threading.current_thread().name,
        }
        return context

    def logging_receive_error(self, message, Nwkid=None, context=None):
        self.logging_receive("Error", message, Nwkid, self.transport_error_context(context))

    def logging_send_error(self, message, Nwkid=None, context=None):
        self.logging_send("Error", message, Nwkid, self.transport_error_context(context))


def open_connection(self):

    if self._transp in ["USB", "DIN", "PI", "V2-USB", "V2-DIN", "V2-PI"]:
        if self._serialPort.find("/dev/") == -1 and self._serialPort.find("COM") == -1:
            Domoticz.Error("Connection Name: Zigate, Transport: Serial, Address: %s" % (self._serialPort))
            return
        Domoticz.Status("Connection Name: Zigate, Transport: Serial, Address: %s" % (self._serialPort))
        if self.pluginconf.pluginConf["byPassDzConnection"] and not self.force_dz_communication:
            result = open_zigate_and_start_reader(self, "serial")
        else:
            self._connection = Domoticz.Connection(
                Name="ZiGate", Transport="Serial", Protocol="None", Address=self._serialPort, Baud=115200
            )
            result = self._connection
        if result:
            start_writer_thread(self)
            start_forwarder_thread(self)

    elif self._transp in ("Wifi", "V2-Wifi"):
        Domoticz.Status(
            "Connection Name: Zigate, Transport: TCP/IP, Address: %s:%s" % (self._serialPort, self._wifiPort)
        )
        if self.pluginconf.pluginConf["byPassDzConnection"] and not self.force_dz_communication:
            result = open_zigate_and_start_reader(self, "tcpip")
        else:
            self._connection = Domoticz.Connection(
                Name="Zigate", Transport="TCP/IP", Protocol="None ", Address=self._wifiAddress, Port=self._wifiPort
            )
            result = self._connection
        if result:
            start_writer_thread(self)
            start_forwarder_thread(self)

    else:
        Domoticz.Error("Unknown Transport Mode: %s" % self._transp)
