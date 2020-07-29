#!/usr/bin/python

import tango
from sardana import State
from sardana.pool.pooldefs import SynchDomain, SynchParam
from sardana.pool.controller import TriggerGateController
from sardana.pool.controller import Type, Description, DefaultValue
from pandaboxlib import PandA
from functools import wraps, partial
import six


def debug_it(func):
    @wraps(func)
    def wrapper(self, *args, **kwargs):

        self._log.debug("Entering {} with args={}, kwargs={}".format(
            func.__name__, args, kwargs))
        output = func(self, *args, **kwargs)
        self._log.debug("Leaving without error {}".format(func.__name__))
        return output
    return wrapper


def handle_error(func=None, msg="Error with PandaBoxCtrl"):
    if func is None:
        return partial(handle_error, msg=msg)
    else:
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            try:
                return func(self, *args, **kwargs)
            except Exception as e:
                six.raise_from(RuntimeError(msg), e)
        return wrapper


class PandaBoxTriggerGateCtrl(TriggerGateController):
    """
    TriggerGateController to control Panda Box.
    """

    organization = "MAX IV"
    gender = "TriggerGate"
    model = "Panda Box"

    ctrl_properties = {
        "pandaboxhostname": {Type: str,
                             Description: "Pandabox hostname"},
        "wait_time": {
            Type: float,
            Description: "Wait time for each pulse in the burst mode. \
                          To take in account of any extra wait time \
                          requirements on HW before sending next pulse.",
            DefaultValue: 0.0},
        "trigger_block": {
            Type: str,
            Description: "Trigger block on Pandabox",
            DefaultValue: "PULSE1"},
        }

    @handle_error(msg="Init: Connection fail to panda box")
    def __init__(self, inst, props, *args, **kwargs):
        TriggerGateController.__init__(self, inst, props, *args, **kwargs)
        self.pandabox = PandA(self.pandaboxhostname)
        self.pandabox.connect_to_panda()

    @debug_it
    def StateOne(self, axis):
        try:
            status_bit = self.pandabox.numquery("{}.QUEUED?".format(
                self.trigger_block))
            if float(status_bit) != 0:
                state = tango.DevState.MOVING
                status = "Triggering"
            else:
                state = tango.DevState.ON
                status = "Standby"
            return state, status
        except Exception as e:
            return tango.DevState.FAULT, "Panda Box is not responding."

    @debug_it
    def PreStartOne(self, axis):
        return True

    @debug_it
    @handle_error(msg="StartOne: Could not Trigger the device:")
    def StartOne(self, axis):
        self.enableBlocks("ONE")

    @debug_it
    @handle_error(msg="AbortOne: Unable to abort PandaBox")
    def AbortOne(self, axis):
        self.enableBlocks("ZERO")

    @debug_it
    def SynchOne(self, axis, configuration):
        self.enableBlocks("ZERO")
        # Configuration
        group = configuration[0]
        trigger_count = group[SynchParam.Repeats]
        # total time for one repeat
        total = group[SynchParam.Total][SynchDomain.Time]
        # integration time (as passed from the scan macros)
        int_time = group[SynchParam.Active][SynchDomain.Time]
        # Configure Panda
        self.configure_panda(trigger_count, total, int_time)

    @debug_it
    @handle_error(msg="Unable to configure_panda")
    def configure_panda(self, trigger_count, total, int_time):
        # set integration time to PULSE block
        self.pandabox.query("{}.DELAY.UNITS=".format(
            self.trigger_block) + "s")
        self.pandabox.query("{}.WIDTH.UNITS=".format(
            self.trigger_block) + "s")
        self.pandabox.query("{}.STEP.UNITS=".format(
            self.trigger_block) + "s")
        self.pandabox.query("{}.ENABLE.DELAY=".format(
            self.trigger_block) + "0")
        self.pandabox.query("{}.TRIG.DELAY=".format(
            self.trigger_block) + "0")
        self.pandabox.query("{}.PULSES=".format(
            self.trigger_block) + str(trigger_count))
        self.pandabox.query("{}.WIDTH=".format(
            self.trigger_block) + str(int_time))
        step = float(int_time) + self.wait_time
        self.pandabox.query("{}.STEP=".format(
            self.trigger_block) + str(step))

    @debug_it
    @handle_error(msg="Error on enableBlocks")
    def enableBlocks(self, value):
        self.pandabox.query("{}.ENABLE=".format(
            self.trigger_block) + value)
        self.pandabox.query("{}.TRIG=".format(
            self.trigger_block) + value)
