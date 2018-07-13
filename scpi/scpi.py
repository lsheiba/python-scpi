"""Generic SCPI commands, allow sending and reading of raw data, helpers to parse information"""
import asyncio
import decimal
import re

from async_timeout import timeout

from .errors import CommandError

COMMAND_DEFAULT_TIMEOUT = 1.0
ERROR_RE = re.compile(r'([+-]\d+),"(.*?)"')


class BitEnum(object):
    """Baseclass for bit definitions of various status registers"""
    @classmethod
    def test_bit(cls, statusvalue, bitname):
        """Test if the given status value has the given bit set"""
        return getattr(cls, bitname) & statusvalue


class ESRBit(BitEnum):
    """Define meanings of the Event Status Register (ESR) bits"""

    @property
    def power_on(self):
        """Power-on. The power has cycled"""
        return 128

    @property
    def user_request(self):
        """User request. The instrument operator has issued a request,
        for instance turning a knob on the front panel."""
        return 64

    @property
    def command_error(self):
        """Command Error. A command error has occurred."""
        return 32

    @property
    def exec_error(self):
        """Execution error. The instrument was not able to execute a command for
        some reason. The reason can be that the supplied data is out of range but
        can also be an external event like a safety switch/knob or some hardware /
        software error."""
        return 16

    @property
    def device_error(self):
        """Device Specific Error."""
        return 8

    @property
    def query_error(self):
        """Query Error. Error occurred during query processing."""
        return 4

    @property
    def control_request(self):
        """Request Control. The instrument is requesting to become active controller."""
        return 2

    @property
    def operation_complete(self):
        """Operation Complete. The instrument has completed all operations.
        This bit is used for synchronisation purposes."""
        return 1


class STBBit(BitEnum):
    """Define meanings of the STatus Byte register (STB) bits"""

    @property
    def rqs_mss(self):
        """RQS, ReQuested Service. This bit is set when the instrument has requested
        service by means of the SeRvice Request (SRQ). When the controller reacts
        by performing a serial poll, the STatus Byte register (STB) is transmitted with
        this bit set. Afand cleared afterwards. It is only set again when a new event
        occurs that requires service.

        MSS, Master Summary Status. This bit is a summary of the STB and the
        SRE register bits 1..5 and 7. Thus it is not cleared when a serial poll occurs.
        It is cleared when the event which caused the setting of MSS is cleared or
        when the corresponding bits in the SRE register are cleared."""
        return 64

    @property
    def rqs(self):
        """alias for rqs_mss"""
        return self.rqs_mss

    @property
    def mss(self):
        """alias for rqs_mss"""
        return self.rqs_mss

    @property
    def esb(self):
        """ESB, Event Summary Bit. This is a summary bit of the standard status
        registers ESR and ESE"""
        return 32

    @property
    def event_summary(self):
        """Alias for esb"""
        return self.esb

    @property
    def mav(self):
        """MAV, Message AVailable. This bit is set when there is data in the output
        queue waiting to be read."""
        return 16

    @property
    def message_available(self):
        """Alias for mav"""
        return self.mav

    @property
    def eav(self):
        """EAV, Error AVailable. This bit is set when there is data in the output
        queue waiting to be read."""
        return 4

    @property
    def error_available(self):
        """Alias for eav"""
        return self.eav


class SCPIProtocol(object):
    """Implements the SCPI protocol talks over the given transport"""
    transport = None
    lock = asyncio.Lock()

    def __init__(self, transport):
        self.transport = transport

    async def quit(self):
        """Shuts down any background threads that might be active"""
        await self.transport.quit()

    async def abort_command(self):
        """Shortcut to the transports abort_command call"""
        await self.transport.abort_command()

    async def get_error(self):
        """Asks for the error code and string"""
        response = await self.ask('SYST:ERR?')
        match = ERROR_RE.search(response)
        if not match:
            # PONDER: Make our own exceptions ??
            raise ValueError("Response '{:s}' does not have correct error format".format(response))
        code = int(match.group(1))
        errstr = match.group(2)
        return (code, errstr)

    async def check_error(self, prev_command=''):
        """Check for error and raise exception if present"""
        code, errstr = await self.get_error()
        if code != 0:
            raise CommandError(prev_command, code, errstr)

    async def command(self, command, cmd_timeout=COMMAND_DEFAULT_TIMEOUT, abort_on_timeout=True):
        """Sends a command, does not wait for response"""
        try:
            with timeout(cmd_timeout):
                with (await self.lock):
                    await self.transport.send_command(command)
        except asyncio.TimeoutError as e:
            # check for the actual error if available
            await self.check_error(command)
            if abort_on_timeout:
                self.abort_command()
            # re-raise the timeout if no other error found
            raise e
        # other errors are allowed to bubble-up as-is

    async def safe_command(self, command, *args, **kwargs):
        """See "command", this just auto-checks for errors each time"""
        await self.command(command, *args, **kwargs)
        await self.check_error(command)

    async def ask(self, command, cmd_timeout=COMMAND_DEFAULT_TIMEOUT, abort_on_timeout=True):
        """Send a command and waits for response, returns the response"""
        try:
            with timeout(cmd_timeout):
                with (await self.lock):
                    await self.transport.send_command(command)
                    return await self.transport.get_response()

        except asyncio.TimeoutError as e:
            # check for the actual error if available
            await self.check_error(command)
            if abort_on_timeout:
                self.abort_command()
            # re-raise the timeout if no other error found
            raise e
        # other errors are allowed to bubble-up as-is

    async def safe_ask(self, command, *args, **kwargs):
        """See "ask", this just autp-checks for errors each time"""
        response = await self.ask(command, *args, **kwargs)
        await self.check_error(command)
        return response


class SCPIDevice(object):
    """Implements nicer wrapper methods for the raw commands from the generic SCPI command set"""
    protocol = None
    transport = None
    command = None
    ask = None

    def __init__(self, protocol, use_safe_variants=True):
        """Initialize device with protocol instance, if use_safe_variants is True (default) then we will
        do the automatic error checking for each command, set to false to take care of it yourself"""
        self.protocol = protocol
        self.transport = self.protocol.transport
        self.command = self.protocol.command
        self.ask = self.protocol.ask
        if use_safe_variants:
            self.command = self.protocol.safe_command
            self.ask = self.protocol.safe_ask

    async def quit(self):
        """Shuts down any background threads that might be active"""
        await self.protocol.quit()

    async def abort(self):
        """Tells the protocol layer to issue "Device clear" to abort the command currently hanging"""
        await self.protocol.abort_command()

    async def get_error(self):
        """Shorthand for procotols method of the same name"""
        return await self.protocol.get_error()

    async def reset(self):
        """Resets the device to known state (with *RST) and clears the error log"""
        return self.protocol.command('*RST;*CLS')

    async def wait_for_complete(self, wait_timeout):
        """Wait for all queued operations to complete (up-to defined timeout)"""
        resp = await self.ask('*WAI;*OPC?', cmd_timeout=wait_timeout)
        return bool(int(resp))

    async def measure_voltage(self, extra_params=""):
        """Returns the measured (scalar) actual output voltage (in volts),
        pass extra_params string to append to the command (like ":ACDC")"""
        resp = await self.ask("MEAS:SCAL:VOLT%s?" % extra_params)
        return decimal.Decimal(resp)

    async def measure_current(self, extra_params=""):
        """Returns the measured (scalar) actual output current (in amps),
        pass extra_params string to append to the command (like ":ACDC")"""
        resp = await self.ask("MEAS:SCAL:CURR%s?" % extra_params)
        return decimal.Decimal(resp)

    async def set_measure_current_max(self, amps):
        """Sets the upper bound (in amps) of current to measure,
        on some devices low-current accuracy can be increased by keeping this low"""
        await self.command("SENS:CURR:RANG %f" % amps)

    async def query_measure_current_max(self):
        """Returns the upper bound (in amps) of current to measure,
        this is not neccessarily same number as set with set_measure_current_max"""
        resp = await self.ask("SENS:CURR:RANG?")
        return decimal.Decimal(resp)

    async def set_voltage(self, millivolts, extra_params=""):
        """Sets the desired output voltage (but does not auto-enable outputs) in millivolts,
        pass extra_params string to append to the command (like ":PROT")"""
        await self.command("SOUR:VOLT%s %f MV" % (extra_params, millivolts))

    async def query_voltage(self, extra_params=""):
        """Returns the set output voltage (in volts),
        pass extra_params string to append to the command (like ":PROT")"""
        resp = await self.ask("SOUR:VOLT%s?" % extra_params)
        return decimal.Decimal(resp)

    async def set_current(self, milliamps, extra_params=""):
        """Sets the desired output current (but does not auto-enable outputs) in milliamps,
        pass extra_params string to append to the command (like ":TRIG")"""
        await self.command("SOUR:CURR%s %f MA" % (extra_params, milliamps))

    async def query_current(self, extra_params=""):
        """Returns the set output current (in amps),
        pass extra_params string to append to the command (like ":TRIG")"""
        resp = await self.ask("SOUR:CURR%s?" % extra_params)
        return decimal.Decimal(resp)

    async def set_output(self, state):
        """Enables/disables output"""
        await self.command("OUTP:STAT %d" % state)

    async def query_output(self):
        """Returns the output state"""
        resp = await self.ask("OUTP:STAT?")
        return bool(int(resp))

    async def identify(self):
        """Returns the identification data, standard order is:
         Manufacturer, Model no, Serial no (or 0), Firmware version"""
        resp = await self.ask("*IDN?")
        return resp.split(',')

    async def query_esr(self):
        """Queries the event status register (ESR) NOTE: The register is cleared when read!
        returns int instead of Decimal like the other number queries since we need to be able
        to do bitwise comparisons"""
        resp = await self.ask("*ESR?")
        return int(resp)

    async def query_ese(self):
        """Queries the event status enable (ESE).
        returns int instead of Decimal like the other number queries since we need to be able
        to do bitwise comparisons"""
        resp = await self.ask("*ESE?")
        return int(resp)

    async def set_ese(self, state):
        """Sets ESE to given value.
        Construct the value with bitwise OR operations using ESRBit properties, for example to enable OPC and exec_error
        error bits in the status flag use: set_ese(ESRBit.operation_complete | ESRBit.exec_error)"""
        await self.command("*ESE %d" % state)

    async def query_sre(self):
        """Queries the service request enable (SRE).
        returns int instead of Decimal like the other number queries since we need to be able
        to do bitwise comparisons"""
        resp = await self.ask("*SRE?")
        return int(resp)

    async def set_sre(self, state):
        """Sets SRE to given value.
        Construct the value with bitwise OR operations using STBBit properties, for example to enable SRQ generation
        on any error or message  use: set_sre(STBBit.mav | STBBit.eav)"""
        await self.command("*SRE %d" % state)

    async def query_stb(self):
        """Queries the status byte (STB).
        returns int instead of Decimal like the other number queries since we need to be able
        to do bitwise comparisons

        If transport implements "serial poll", will use that instead of SCPI query to get the value"""
        try:
            resp = await self.transport.poll()
        except AttributeError:
            resp = await self.ask("*STB?")
        return int(resp)
