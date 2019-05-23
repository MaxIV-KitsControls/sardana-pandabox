#!/usr/bin/env python
from pandaboxlib import PandA
import time
import socket
from multiprocessing.pool import ThreadPool

from sardana import State, DataAccess
from sardana.sardanavalue import SardanaValue
from sardana.pool import AcqSynch
from sardana.pool.controller import CounterTimerController, Type, Access, \
    Description, Memorize, Memorized, MemorizedNoInit, NotMemorized

__all__ = ['PandaboxCoTiCtrl']

class PandaboxCoTiCtrl(CounterTimerController):

    MaxDevice = 10 # TODO remove this or check pandabox maximum number of channels

    ctrl_properties = {'PandaboxHost': {'Description': 'Pandabox Host name',
                                      'Type': 'PyTango.DevString'},
                       }

    ctrl_attributes = {
        }

    axis_attributes = {"ChannelName": {
                            Type: str,
                            Description: 'Channel name from pandabox',
                            Memorize: NotMemorized,
                            Access: DataAccess.ReadWrite,
                            },
                       "AcquisitionMode": {
                            Type: str,
                            Description: 'Acquisition mode: value, mean, min, max,...',
                            Memorize: NotMemorized,
                            Access: DataAccess.ReadWrite,
                            },
                       }

    def __init__(self, inst, props, *args, **kwargs):
        """Class initialization."""
        CounterTimerController.__init__(self, inst, props, *args, **kwargs)
        self._log.debug("__init__(%s, %s): Entering...", repr(inst),
                        repr(props))

        try:
            self.pandabox = PandA(self.PandaboxHost)
            self.pandabox.connect_to_panda()
        except (NameError, socket.gaierror):
            raise Exception('Unable to connect to PandABox.') 
            

        self.data_pool = ThreadPool(processes=1)
        self.async_result = None

        self.index = 0
        self._repetitions = 0

    def AddDevice(self, axis):
        """Add device to controller."""
        self._log.debug("AddDevice(%d): Entering...", axis)
        # count buffer for the continuous scan
        if axis != 1:
            self.index = 0

    def DeleteDevice(self, axis):
        """Delete device from the controller."""
        self._log.debug("DeleteDevice(%d): Entering...", axis)
        self.pandabox.disconnect_from_panda()

    def StateAll(self):
        """Read state of all axis."""
        # self._log.debug("StateAll(): Entering...")
        state = self.pandabox.query('*PCAP.STATUS?')

        if "Busy" in state:
            self.state = State.Moving

        elif "Idle" in state:
            self.state = State.On

        elif "OK" not in state:    #TODO check if there is always OK
            self.state = State.Fault

        else:
            self.state = State.Fault
            self._log.debug("StateAll(): %r %r UNKNWON STATE: %s" % (
                self.state, self.status), state)
        self.status = state
        # self._log.debug("StateAll(): %r %r" %(self.state, self.status))

    def StateOne(self, axis):
        """Read state of one axis."""
        # self._log.debug("StateOne(%d): Entering...", axis)
        return self.state, self.status

    def LoadOne(self, axis, value, repetitions):
        # self._log.debug("LoadOne(%d, %f, %d): Entering...", axis, value,
        #                 repetitions)

        # Set Integration time in s
        self.pandabox.query('PULSE1.WIDTH.UNITS=s')
        self.pandabox.query('PULSE1.WIDTH=%f' % (value))

        if self._synchronization in [AcqSynch.SoftwareTrigger,
                                     AcqSynch.SoftwareGate]:
            # self._log.debug("SetCtrlPar(): setting synchronization "
            #                 "to SoftwareTrigger")
            self._repetitions = 1
            source = 'SOFTWARE'

        else:
            # self._log.debug("SetCtrlPar(): setting synchronization "
            #                 "to HardwareTrigger")
            source = 'HARDWARE'
            self._repetitions = repetitions

        # TODO: find a way to differ between software and hardware
        # trigger in pandabox:
#        self.sendCmd('TRIG:MODE %s' % source, rw=False)
#        # Set Number of Triggers
#        self.sendCmd('ACQU:NTRI %r' % self._repetitions, rw=False)

    def PreStartOneCT(self, axis):
        # self._log.debug("PreStartOneCT(%d): Entering...", axis)
        if axis != 1:
            self.index = 0
        return True

    def StartAllCT(self):
        """
        Starting the acquisition is done only if before was called
        PreStartOneCT for master channel.
        """
        # self._log.debug("StartAllCT(): Entering...")
        cmd = '*PCAP:ARM='
        # if self._synchronization in [AcqSynch.SoftwareTrigger,
        #                              AcqSynch.SoftwareGate]:
        self.pandabox.query(cmd)

        self.async_result = self.data_pool.apply_async(get_data, args = (self.PandaboxHost, 8889))

        # THIS PROTECTION HAS TO BE REVIEWED
        # FAST INTEGRATION TIMES MAY RAISE WRONG EXCEPTIONS
        # e.g. 10ms ACQTIME -> self.state MAY BE NOT MOVING BECAUSE
        # FINISHED, NOT FAILED
        self.StateAll()
        t0 = time.time()
        while (self.state != State.Moving):
            if time.time() - t0 > 3:
                raise Exception('The HW did not start the acquisition')
            self.StateAll()
        return True

    def ReadAll(self):
        # self._log.debug("ReadAll(): Entering...")
        data_ready = int(self.pandabox.numquery('*PCAP.CAPTURED?'))
        self.new_data = self.async_result.get()

        #TODO handle header in first lines

#TODO: try to understand the code commented bellow
# this part is to return data while acquisition is ongoing?
# self.index?

#        try:
#            if self.index < data_ready:
#                data_len = data_ready - self.index
#                # THIS CONTROLLER IS NOT YET READY FOR TIMESTAMP DATA
#                self.sendCmd('TMST 0', rw=False)
#                raw_data = self.sendCmd('ACQU:MEAS? %r,%r' % (self.index-1,
#                                                              data_len))
#                data = eval(raw_data)
#                for chn_name, values in data:
#                    self.new_data.append(values)
#                time_data = [self.itime] * len(self.new_data[0])
#                self.new_data.insert(0, time_data)
#                if self._repetitions != 1:
#                    self.index += len(time_data)
#
#        except Exception as e:
#            raise Exception("ReadAll error: %s: "+str(e))

    def ReadOne(self, axis):
        # self._log.debug("ReadOne(%d): Entering...", axis)
        if len(self.new_data) == 0:
            return []

        if self._synchronization in [AcqSynch.SoftwareTrigger,
                                     AcqSynch.SoftwareGate]:
            return SardanaValue(self.new_data[axis-1][0])
        else:
            val = self.new_data[axis-1]
            return val

    def AbortOne(self, axis):
        # self._log.debug("AbortOne(%d): Entering...", axis)
        self.pandabox.query('*PCAP.DISARM')

    # listener to tcp port where data is streamed from pandabox
    def get_data(self, host, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((host, port))
        s.sendall('\n')
        data = s.recv(1024)
        raw_data = ""
        while True:
    #        print 'data:', repr(data)
            data = s.recv(1024)
            raw_data += data 
            if "END" in repr(data):
                break
        s.close()
    #    print 'Received data:', repr(raw_data)
        return raw_data

###############################################################################
#                Axis Extra Attribute Methods
###############################################################################
    def GetExtraAttributePar(self, axis, name):
        self._log.debug("GetExtraAttributePar(%d, %s): Entering...", axis,
                        name)
        if axis == 1:
            raise ValueError('The axis 1 does not use the extra attributes')

        name = name.lower()
        axis -= 1
        try:
            if name == "range":
                cmd = 'CHAN{0:02d}:CABO:RANGE?'.format(axis)
                return self.sendCmd(cmd)

            elif name == 'inversion':
                cmd = 'CHAN{0:02d}:CABO:INVE?'.format(axis)
                return eval(self.sendCmd(cmd))

            elif name == 'instantcurrent':
                cmd = 'CHAN{0:02d}:INSCurrent?'.format(axis)
                return eval(self.sendCmd(cmd))

            elif name == 'filter':
                cmd = 'CHAN{0:02d}:CABO:FILTER?'.format(axis)
                return self.sendCmd(cmd)

        except Exception, e:
            error_msg = 'Error getting extra attribute {0} \
                         with error: {1}'.format(name, e)
            raise Exception(error_msg)

    def SetExtraAttributePar(self, axis, name, value):
        if axis == 1:
            raise ValueError('The axis 1 does not use the extra attributes')

        name = name.lower()
        axis -= 1
        if name == "range":
            # TODO: Add this check to its own method
            possible_values = ['AUTO',
                               '1mA', '100uA', '10uA', '1uA',
                               '100nA', '10nA', '1nA', '100pA']
            if value not in possible_values:
                error_msg = "Range value not acceptable, please chose one of the following \
                             {0}".format(possible_values)
                raise Exception(error_msg)
            else:
                cmd = 'CHAN{0:02d}:CABO:RANGE {1}'.format(axis, value)
                self.sendCmd(cmd, rw=False)
        
        elif name == 'inversion':
            cmd = 'CHAN{0:02d}:CABO:INVE {1}'.format(axis, int(value))
            self.sendCmd(cmd, rw=False)
        elif name == 'filter':
            # TODO: Add this check to its own method
            possible_values = ['3200Hz', '100Hz', '10Hz', '1Hz', '0.5Hz']
            if value not in possible_values:
                error_msg = "Filter value not acceptable, please chose one of the following \
                             {0}".format(possible_values)
                raise Exception(error_msg)
            else:
                cmd = 'CHAN{0:02d}:CABO:FILTER {1}'.format(axis, value)
                self.sendCmd(cmd, rw=False)


###############################################################################
#                Controller Extra Attribute Methods
###############################################################################
    def SetCtrlPar(self, parameter, value):
        pass

    def GetCtrlPar(self, parameter):
        pass

if __name__ == '__main__':
    host = 'b308a-cab04-pandabox-temp-0'
    ctrl = PandaboxCoTiCtrl('test', {'PandaboxHost': host})
    ctrl.AddDevice(1)
    ctrl.AddDevice(2)
    ctrl.AddDevice(3)
    ctrl.AddDevice(4)
    ctrl.AddDevice(5)

    ctrl._synchronization = AcqSynch.SoftwareTrigger
    # ctrl._synchronization = AcqSynch.HardwareTrigger
    acqtime = 1.1
    ctrl.LoadOne(1, acqtime, 10)
    ctrl.StartAllCT()
    t0 = time.time()
    ctrl.StateAll()
    while ctrl.StateOne(1)[0] != State.On:
        ctrl.StateAll()
        time.sleep(0.1)
    print time.time() - t0 - acqtime
    ctrl.ReadAll()
    print ctrl.ReadOne(2)
