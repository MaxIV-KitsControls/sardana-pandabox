#!/usr/bin/env python
from pandaboxlib import PandA
import time
import socket
from sockio.py2 import TCP 
from StringIO import StringIO 
import numpy as np 

from sardana import State, DataAccess
from sardana.sardanavalue import SardanaValue
from sardana.pool import AcqSynch
from sardana.pool.controller import CounterTimerController, Type, Access, \
    Description, Memorize, Memorized, MemorizedNoInit, NotMemorized, DefaultValue

__all__ = ['PandaboxCoTiCtrl']

class PandaboxCoTiCtrl(CounterTimerController):

    MaxDevice = 28 # TODO remove this or check pandabox maximum number of channels

    ctrl_properties = {'PandaboxHost': {'Description': 'Pandabox Host name',
                                      'Type': 'PyTango.DevString'},
                       'PcapEnable': {'Description': 'Hardware trigger config: PCAP.ENABLE',
                                      'Type': 'PyTango.DevString'},
                       'PcapGate': {'Description': 'Hardware trigger config: PCAP.GATE',
                                      'Type': 'PyTango.DevString'},
                       'PcapTrig': {'Description': 'Hardware trigger config: PCAP.TRIG',
                                      'Type': 'PyTango.DevString'},
                       }

    ctrl_attributes = {
        }

    axis_attributes = {"ChannelName": {
                            Type: str,
                            Description: 'Channel name from pandabox',
                            Memorize: Memorized,
                            Access: DataAccess.ReadWrite,
                            },
                       "AcquisitionMode": {
                            Type: str,
                            Description: 'Acquisition mode: value, mean, min, max,...',
                            Memorize: Memorized,
                            DefaultValue: 'Value',
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

        # make sure PCAP block is reset
        self.pandabox.query('PCAP.ENABLE=ZERO')
        self.pandabox.query('*PCAP.DISARM=')

        try:
            self.data_socket = TCP(self.PandaboxHost, 8889, timeout=3)
            self.data_socket.open()
        except:
            raise Exception('Unable to open PandABox data stream.') 
 
        # check if data stream starts correctly
        ack = self.data_socket.write_readline('\n')
        if "OK" not in ack:
            raise Exception('Acknowledge to data stream failed!') 
        print "PandaboxCoTiCtrl: data stream listener starts...", ack
        self.data_buffer = ""
        self.header_okay_flag = False
        self.data_end_flag = False 

        self.attributes = {}
        self.hw_trigger_cfg = {}
        self.hw_trigger_cfg['enable'] = self.PcapEnable
        self.hw_trigger_cfg['gate'] = self.PcapGate
        self.hw_trigger_cfg['trig'] = self.PcapTrig

        self.channels_order = []

        # channels and modes available
        self._modes = ['Value','Diff','Min','Max','Sum','Mean']
        self._channels = [
            'INENC1.VAL','INENC2.VAL','INENC3.VAL','INENC4.VAL',
            'CALC1.OUT','CALC2.OUT','COUNTER1.OUT','COUNTER2.OUT',
            'COUNTER3.OUT','COUNTER4.OUT','COUNTER5.OUT','COUNTER6.OUT',
            'COUNTER7.OUT','COUNTER8.OUT','FILTER1.OUT','FILTER2.OUT',
            'PGEN1.OUT','PGEN2.OUT','QDEC.OUT','FMC_ACQ427_IN.VAL1',
            'FMC_ACQ427_IN.VAL2','FMC_ACQ427_IN.VAL3','FMC_ACQ427_IN.VAL4',
            'FMC_ACQ427_IN.VAL5','FMC_ACQ427_IN.VAL6','FMC_ACQ427_IN.VAL7',
            'FMC_ACQ427_IN.VAL8','PCAP.SAMPLES']

        self.index = 0
        self._repetitions = 0

    def AddDevice(self, axis):
        """Add device to controller."""
        self._log.debug("AddDevice(%d): Entering...", axis)
        self.attributes[axis-1] = {'ChannelName': None, 'AcquisitionMode':'Value'}
        # count buffer for the continuous scan
        if axis != 1:
            self.index = 0

    def DeleteDevice(self, axis):
        """Delete device from the controller."""
        self._log.debug("DeleteDevice(%d): Entering...", axis)
        self.data_socket.close()
        self.pandabox.disconnect_from_panda()

    def StateAll(self):
        """Read state of all axis."""
        # self._log.debug("StateAll(): Entering...")
        state = self.pandabox.query('*PCAP.STATUS?')

        if "Busy" in state:
            self.state = State.Moving
        elif "Idle" in state:
            self.state = State.On
        elif "OK" not in state:
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
        if axis != 1:
            raise Exception('The master channel should be the axis 1')

        self.itime = value
        self.index = 0 

        # Set Integration time in s per point
        self.pandabox.query('PULSE1.WIDTH.UNITS=s')
        if value < 8e-08:   # minimum reliable integration time is
                            # 10 FPGA clock ticks 125 MHz -> 80 ns 
            self._log.debug("The minimum integration time is 80 ns")
            value = 8e-08
        self.pandabox.query('PULSE1.WIDTH=%.9f' % (value))

        # Set falling edge capture to have
        # "gate signal that marks the capture boundaries"
        # in order to respect the integration time
        # see PCAP block documentation
        self.pandabox.query('PCAP.TRIG_EDGE=Falling')

        if self._synchronization in [AcqSynch.SoftwareTrigger,
                                     AcqSynch.SoftwareGate]:
            # self._log.debug("SetCtrlPar(): setting synchronization "
            #                 "to SoftwareTrigger")
            self._repetitions = 1
            self.pandabox.query('PULSE1.PULSES=1')

            # link blocks for software acquisition
            trig = 'PULSE1.OUT'
        else:
            # self._log.debug("SetCtrlPar(): setting synchronization "
            #                 "to HardwareTrigger")
            self._repetitions = repetitions
            # link blocks for hardware acquisition
            trig = self.hw_trigger_cfg['trig']

        # create links to PCAP block
        # TODO: separate gated mode? 
        #self.pandabox.query('PCAP.GATE=ONE')
        self.pandabox.query('PCAP.GATE='+trig)
        self.pandabox.query('PCAP.TRIG='+trig)
        
        # reset data buffer and header flag
        self.header_okay_flag = False
        self.data_end_flag = False 
        self.data_buffer = '' 

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
        ret = self.pandabox.query('*PCAP.ARM=')
        if "OK" not in ret:
            print "Pandabox arm PCAP failed. Disarm and arm again..."
            ret = self.pandabox.query('*PCAP.DISARM=')
            ret = self.pandabox.query('*PCAP.ARM=')

        # start acquisition by enabling PCAP
        self.pandabox.query('PCAP.ENABLE=ONE')

        # trig acquisition
        if self._synchronization in [AcqSynch.SoftwareTrigger,
                                     AcqSynch.SoftwareGate]:
            self.pandabox.query('PULSE1.TRIG=ZERO')
            self.pandabox.query('PULSE1.TRIG=ONE')
        # else wait for triggers (hardware mode)

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
        self.data_ready = int(self.pandabox.numquery('*PCAP.CAPTURED?'))
        #print "Points acquired: %d"%self.data_ready

        self.new_data = [] 
        #self.index = 0 

        if self.data_ready == 0:
            print "Pandabox: No data available yet."
            self._ParseHeader()
            return
        elif self.data_ready <= self._repetitions:
            if self.data_ready == self._repetitions:
                print "Pandabox data acquisition has finished, disabling PCAP..."
                self.pandabox.query('PCAP.ENABLE=ZERO') # it disarms PCAP too
            try:
                if not self.data_end_flag:
                    data = self.data_socket.readline()
                    if 'END' not in data:
                        self.data_buffer += data
                    else:
                        print "Pandabox data acquisition ENDs okay!"
                        self.data_end_flag = True 
            except socket.error, e:
                print "Pandabox: data socket error: ", e
                self.data_socket.close()

            data_only = np.genfromtxt(StringIO(self.data_buffer), dtype='float64')

            # TODO: hw scan mode fails with int time < 100 ms
            # we have only one line per execution of this method, fix it
            if data_only.ndim > 1: # crop to get only new data
                data_only = data_only[self.index:self.data_ready+1]

            if self.index <= self.data_ready:   # mandatory to avoid extra lines
                self.new_data = np.ndarray.tolist(data_only.transpose())
                if type(self.new_data[0]) != list:
                    one_line_data = []
                    for value in self.new_data:
                        one_line_data.append([value])
                    self.new_data = one_line_data 
    
                time_data = [self.itime] * len(self.new_data[0])
                self.new_data.insert(0, time_data)
                
                if self._repetitions != 1:
                    self.index += len(time_data)


    def ReadOne(self, axis):
        # self._log.debug("ReadOne(%d): Entering...", axis)
        if len(self.new_data) == 0:
            #return -1 
            return None 

        if axis == 1:    # timer axis
            channel_index = 0
        else:
            channel_name = str(self.attributes[axis-1]['ChannelName'])
            if channel_name not in self.channels_order:
                raise ValueError('Channel name configured is not enabled in pandabox')
            else:
                channel_index = (self.channels_order.index(channel_name) + 1) # +1 because of timer column
            
        if self._synchronization in [AcqSynch.SoftwareTrigger,
                                     AcqSynch.SoftwareGate]:
            return SardanaValue(self.new_data[channel_index][0])

        else:
            val = self.new_data[channel_index]
            return val

    def AbortOne(self, axis):
        # self._log.debug("AbortOne(%d): Entering...", axis)
        self.pandabox.query('*PCAP.DISARM=')

    def StopOne(self, axis):
        # self._log.debug("StopOne(%d): Entering...", axis)
        self.pandabox.query('PCAP.ENABLE=ZERO')
        self.pandabox.query('*PCAP.DISARM=')

    def _ParseHeader(self):
        if not self.header_okay_flag:
            # Prepare for data receiving, parse header after arm
            # HEADER FORMAT:
            # missed: 0
            # process: Scaled
            # format: ASCII
            # fields:
            #  + one line per channel enabled
            fixed_header_lines = 4 
            num_channels_enabled = self.pandabox.get_number_channels()
            num_lines = fixed_header_lines+num_channels_enabled
    
            data_header = ""
            try:
                data_header = self.data_socket.readlines(num_lines+1) #+1 blank line
                if "fields" in data_header[3]:
                    print "Pandabox data header parsing okay!"
                    self.header_okay_flag = True 
            except socket.error, e:
                print "Pandabox: socket error header!!!! = ", e
                self.header_okay_flag = False
                pass 
    
            channels_list = data_header[fixed_header_lines:fixed_header_lines+num_channels_enabled]
            self.channels_order = []
            for channel in channels_list:
                channel = channel.split(' ')[1:2]
                self.channels_order.append(channel[0])
            #print "Channels order: ", self.channels_order
        return

###############################################################################
#                Axis Extra Attribute Methods
###############################################################################
    def GetExtraAttributePar(self, axis, name):
        self._log.debug("GetExtraAttributePar(%d, %s): Entering...", axis,
                        name)
        if axis == 1:
           raise ValueError('The axis 1 does not use the extra attributes')

        axis -= 1
        return self.attributes[axis][name]


    def SetExtraAttributePar(self, axis, name, value):
        if axis == 1:
            raise ValueError('The axis 1 does not use the extra attributes')

        axis -= 1
        # TODO: Add this check to its own method
        if name == 'AcquisitionMode' and value not in self._modes:
                error_msg = "AcquisitionMode value not acceptable, please chose one of the following \
                             {0}".format(self._modes)
                raise Exception(error_msg)
        elif name == 'ChannelName' and value not in self._channels:
                error_msg = "ChannelName value not acceptable, please chose one of the following \
                             {0}".format(self._channels)
                raise Exception(error_msg)
        else:
            # first disable previous channel
            if self.attributes[axis]['ChannelName'] is not None:
                cmd = self.attributes[axis]['ChannelName'] + '.CAPTURE=No'
                self.pandabox.query(cmd)
            self.attributes[axis][name] = value
            cmd = self.attributes[axis]['ChannelName'] + '.CAPTURE=' + self.attributes[axis]['AcquisitionMode']
            self.pandabox.query(cmd)
        

###############################################################################
#                Controller Extra Attribute Methods
###############################################################################
    # MANDATORY implement it to have self._synchronization
    def SetCtrlPar(self, parameter, value):
        CounterTimerController.SetCtrlPar(self, parameter, value)

    def GetCtrlPar(self, parameter):
        value = CounterTimerController.GetCtrlPar(self, parameter)
        return value

if __name__ == '__main__':
    host = 'w-kitslab-pandabox-0'
    enable = 'ONE'
    gate = 'ONE'
    trig = 'PULSE1.OUT'
    ctrl = PandaboxCoTiCtrl('test', {'PandaboxHost': host, 'PcapEnable': enable,'PcapGate': gate, 'PcapTrig': trig})
    ctrl.AddDevice(1)
    ctrl.AddDevice(2)
    ctrl.SetExtraAttributePar(2, "ChannelName", "COUNTER1.OUT")
    ctrl.AddDevice(3)
    ctrl.SetExtraAttributePar(3, "ChannelName", "INENC1.VAL")
    #ctrl.AddDevice(4)
    #ctrl.AddDevice(5)

    acqtime = 0.5
    ctrl._synchronization = AcqSynch.SoftwareTrigger
    #ctrl._synchronization = AcqSynch.HardwareTrigger
    if ctrl._synchronization == AcqSynch.SoftwareTrigger:
        repetitions = 1
    else:
        repetitions = 3
    ctrl.LoadOne(1, acqtime, repetitions)
    ctrl.StartAllCT()
    t0 = time.time()
    ctrl.StateAll()
    while ctrl.StateOne(1)[0] != State.On:
        ctrl.StateAll()
        ctrl.ReadAll()
        time.sleep(0.25)
    print "Time: ", time.time() - t0 - acqtime
    print "COUNTER1.OUT = ", ctrl.ReadOne(2)
    print "INENC1.VAL = ", ctrl.ReadOne(3)


