import argparse
import logging
import serial


def remove_crud(string):
    """Return string without useless information.

     Return string with trailing zeros after a decimal place, trailing
     decimal points, and leading and trailing spaces removed.
     """
    if "." in string:
        string = string.rstrip("0")

    string = string.lstrip("0 ")
    string = string.rstrip(" .")

    return string


class Chain(serial.Serial):
    """Create Chain object.

    Harvard syringe pumps are daisy chained together in a 'pump chain'
    off a single serial port. A pump address is set on each pump. You
    must first create a chain to which you then add Pump objects.

    Chain is a subclass of serial.Serial. Chain creates a serial.Serial
    instance with the required parameters, flushes input and output
    buffers (found during testing that this fixes a lot of problems) and
    logs creation of the Chain.
    """

    def __init__(self, port, stopbits=serial.STOPBITS_TWO):
        serial.Serial.__init__(
            self, port=port, stopbits=stopbits, parity=serial.PARITY_NONE, timeout=2,
        )
        self.flushOutput()
        self.flushInput()
        logging.info("Chain created on %s", port)


class Pump:
    """Create Pump object for Harvard Pump 11.

    Argument:
        Chain: pump chain

    Optional arguments:
        address: pump address. Default is 0.
        name: used in logging. Default is Pump 11.
    """

    def __init__(self, chain, address=0, name="Pump 11"):
        self.name = name
        self.serial = chain
        self.address = "{0:02.0f}".format(address)
        self.diameter = None
        self.flowrate = None
        self.targetvolume = None

        """Query model and version number of firmware to check pump is
        OK. Responds with a load of stuff, but the last three characters
        are XXY, where XX is the address and Y is pump status. :, > or <
        when stopped, running forwards, or running backwards. Confirm
        that the address is correct. This acts as a check to see that
        the pump is connected and working."""
        try:
            self.write("VER")
            resp = self.read(17)

            if int(resp[-3:-1]) != int(self.address):
                raise PumpError("No response from pump at address %s" % self.address)
        except PumpError:
            self.serial.close()
            raise

        logging.info(
            "%s: created at address %s on %s", self.name, self.address, self.serial.port,
        )

    def __repr__(self):
        string = ""
        for attr in self.__dict__:
            string += "%s: %s\n" % (attr, self.__dict__[attr])
        return string

    def write(self, command):
        self.serial.write(self.address + command + "\r")

    def read(self, num_bytes=5):
        response = self.serial.read(num_bytes)

        if len(response) == 0:
            raise PumpError("%s: no response to command" % self.name)
        return response

    def setdiameter(self, diameter):
        """Set syringe diameter (millimetres).

        Pump 11 syringe diameter range is 0.1-35 mm. Note that the pump
        ignores precision greater than 2 decimal places. If more d.p.
        are specificed the diameter will be truncated.
        """
        if diameter > 35 or diameter < 0.1:
            raise PumpError("%s: diameter %s mm is out of range" % (self.name, diameter))

        # TODO: Got to be a better way of doing this with string formatting
        diameter = str(diameter)

        # Pump only considers 2 d.p. - anymore are ignored
        if len(diameter) > 5:
            if diameter[2] == ".":  # e.g. 30.2222222
                diameter = diameter[0:5]
            elif diameter[1] == ".":  # e.g. 3.222222
                diameter = diameter[0:4]

            diameter = remove_crud(diameter)
            logging.warning("%s: diameter truncated to %s mm", self.name, diameter)
        else:
            diameter = remove_crud(diameter)

        # Send command
        self.write("MMD" + diameter)
        resp = self.read(5)

        # Pump replies with address and status (:, < or >)
        if resp[-1] == ":" or resp[-1] == "<" or resp[-1] == ">":
            # check if diameter has been set correctlry
            self.write("DIA")
            resp = self.read(15)
            returned_diameter = remove_crud(resp[3:9])

            # Check diameter was set accurately
            if returned_diameter != diameter:
                logging.error(
                    "%s: set diameter (%s mm) does not match diameter"
                    " returned by pump (%s mm)",
                    self.name,
                    diameter,
                    returned_diameter,
                )
            elif returned_diameter == diameter:
                self.diameter = float(returned_diameter)
                logging.info("%s: diameter set to %s mm", self.name, self.diameter)
        else:
            raise PumpError("%s: unknown response to setdiameter" % self.name)

    def setflowrate(self, flowrate):
        """Set flow rate (microlitres per minute).

        Flow rate is converted to a string. Pump 11 requires it to have
        a maximum field width of 5, e.g. "XXXX." or "X.XXX". Greater
        precision will be truncated.

        The pump will tell you if the specified flow rate is out of
        range. This depends on the syringe diameter. See Pump 11 manual.
        """
        flowrate = str(flowrate)

        if len(flowrate) > 5:
            flowrate = flowrate[0:5]
            flowrate = remove_crud(flowrate)
            logging.warning("%s: flow rate truncated to %s uL/min", self.name, flowrate)
        else:
            flowrate = remove_crud(flowrate)

        self.write("ULM" + flowrate)
        resp = self.read(5)

        if resp[-1] == ":" or resp[-1] == "<" or resp[-1] == ">":
            # Flow rate was sent, check it was set correctly
            self.write("RAT")
            resp = self.read(150)
            returned_flowrate = remove_crud(resp[2:8])

            if returned_flowrate != flowrate:
                logging.error(
                    "%s: set flowrate (%s uL/min) does not match"
                    "flowrate returned by pump (%s uL/min)",
                    self.name,
                    flowrate,
                    returned_flowrate,
                )
            elif returned_flowrate == flowrate:
                self.flowrate = returned_flowrate
                logging.info("%s: flow rate set to %s uL/min", self.name, self.flowrate)
        elif "OOR" in resp:
            raise PumpError(
                "%s: flow rate (%s uL/min) is out of range" % (self.name, flowrate)
            )
        else:
            raise PumpError("%s: unknown response" % self.name)

    def infuse(self):
        """Start infusing pump."""
        self.write("RUN")
        resp = self.read(5)
        while resp[-1] != ">":
            if resp[-1] == "<":  # wrong direction
                self.write("REV")
            else:
                raise PumpError("%s: unknown response to to infuse" % self.name)
            resp = self.serial.read(5)

        logging.info("%s: infusing", self.name)

    def withdraw(self):
        """Start withdrawing pump."""
        self.write("REV")
        resp = self.read(5)

        while resp[-1] != "<":
            if resp[-1] == ":":  # pump not running
                self.write("RUN")
            elif resp[-1] == ">":  # wrong direction
                self.write("REV")
            else:
                raise PumpError("%s: unknown response to withdraw" % self.name)
            resp = self.read(5)

        logging.info("%s: withdrawing", self.name)

    def stop(self):
        """Stop pump."""
        self.write("STP")
        resp = self.read(5)

        if resp[-1] != ":":
            raise PumpError("%s: unexpected response to stop" % self.name)
        logging.info("%s: stopped", self.name)

    def settargetvolume(self, targetvolume):
        """Set the target volume to infuse or withdraw (microlitres)."""
        self.write("MLT" + str(targetvolume))
        resp = self.read(5)

        # response should be CRLFXX:, CRLFXX>, CRLFXX< where XX is address
        # Pump11 replies with leading zeros, e.g. 03, but PHD2000 misbehaves and
        # returns without and gives an extra CR. Use int() to deal with
        if resp[-1] == ":" or resp[-1] == ">" or resp[-1] == "<":
            self.targetvolume = float(targetvolume)
            logging.info("%s: target volume set to %s uL", self.name, self.targetvolume)
        else:
            raise PumpError("%s: target volume not set" % self.name)

    def waituntiltarget(self):
        """Wait until the pump has reached its target volume."""
        logging.info("%s: waiting until target reached", self.name)
        # counter - need it to check if it's the first loop
        i = 0

        while True:
            # Read once
            self.serial.write(self.address + "VOL\r")
            resp1 = self.read(15)

            if ":" in resp1 and i == 0:
                raise PumpError(
                    "%s: not infusing/withdrawing - infuse or "
                    "withdraw first" % self.name
                )
            if ":" in resp1 and i != 0:
                # pump has already come to a halt
                logging.info("%s: target volume reached, stopped", self.name)
                break

            # Read again
            self.serial.write(self.address + "VOL\r")
            resp2 = self.read(15)

            # Check if they're the same - if they are, break, otherwise continue
            if resp1 == resp2:
                logging.info("%s: target volume reached, stopped", self.name)
                break

            i = i + 1


class PHD2000(Pump):
    """Harvard PHD2000 pump object.

    Inherits from Pump class, but needs its own class as it doesn't
    stick to the Pump 11 protocol with commands to stop and set the
    target volume.
    """

    def stop(self):
        """Stop pump."""
        self.write("STP")
        resp = self.read(5)

        if resp[-1] == "*":
            logging.info("%s: stopped", self.name)
        else:
            raise PumpError("%s: unexpected response to stop" % self.name)

    def settargetvolume(self, targetvolume):
        """Set the target volume to infuse or withdraw (microlitres)."""

        # PHD2000 expects target volume in mL not uL like the Pump11, so convert to mL
        targetvolume = str(float(targetvolume) / 1000.0)

        if len(targetvolume) > 5:
            targetvolume = targetvolume[0:5]
            logging.warning(
                "%s: target volume truncated to %s mL", self.name, targetvolume
            )

        self.write("MLT" + targetvolume)
        resp = self.read(5)

        # response should be CRLFXX:, CRLFXX>, CRLFXX< where XX is address
        # Pump11 replies with leading zeros, e.g. 03, but PHD2000 misbehaves and
        # returns without and gives an extra CR. Use int() to deal with
        if resp[-1] == ":" or resp[-1] == ">" or resp[-1] == "<":
            # Been set correctly, so put it back in the object (as uL, not mL)
            self.targetvolume = float(targetvolume) * 1000.0
            logging.info("%s: target volume set to %s uL", self.name, self.targetvolume)


class MightyMini:
    def __init__(self, chain, name="Mighty Mini"):
        self.name = name
        self.serial = chain.serial
        self.flowrate = None
        logging.info("%s: created on %s", self.name, self.serial.port)

    def __repr__(self):
        string = ""
        for attr in self.__dict__:
            string += "%s: %s\n" % (attr, self.__dict__[attr])
        return string

    def setflowrate(self, flowrate):
        flowrate = int(flowrate)
        if flowrate > 9999:
            flowrate = 9999
            logging.warning("%s: flow rate limited to %s uL/min", self.name, flowrate)

        self.serial.write("FM" + "{:04d}".format(flowrate))
        resp = self.serial.read(3)
        self.serial.flushInput()
        if len(resp) == 0:
            raise PumpError("%s: no response to set flowrate" % self.name)
        if resp[0] == "O" and resp[1] == "K":
            # flow rate sent, check it is correct
            self.serial.write("CC")
            resp = self.serial.read(11)
            returned_flowrate = int(float(resp[5:-1]) * 1000)
            if returned_flowrate != flowrate:
                raise PumpError(
                    "%s: set flowrate (%s uL/min) does not match"
                    " flowrate returned by pump (%s uL/min)"
                    % (self.name, flowrate, returned_flowrate)
                )
            if returned_flowrate == flowrate:
                self.flowrate = returned_flowrate
                logging.info("%s: flow rate set to %s uL/min", self.name, self.flowrate)
        else:
            raise PumpError(
                "%s: error setting flow rate (%s uL/min)" % (self.name, flowrate)
            )

    def infuse(self):
        self.serial.write("RU")
        resp = self.serial.read(3)
        if len(resp) == 0:
            raise PumpError("%s: no response to infuse" % self.name)
        if resp[0] == "O" and resp[1] == "K":
            logging.info("%s: infusing", self.name)

    def stop(self):
        self.serial.write("ST")
        resp = self.serial.read(3)
        if len(resp) == 0:
            raise PumpError("%s: no response to stop" % self.name)
        if resp[0] == "O" and resp[1] == "K":
            logging.info("%s: stopping", self.name)


class Pump33:
    """Create Pump object for Harvard Pump 33.

    Argument:
        Chain: pump chain

    Optional arguments:
        address: pump address. Default is 0.
        name: used in logging. Default is Pump 33.
    """

    def __init__(self, chain, address=0, name="Pump 33"):
        self.name = name
        self.serial = chain
        self.address = "{0:02.0f}".format(address)
        self.mode = None
        self.diameter1 = None
        self.diameter2 = None
        self.flowrate1 = None
        self.flowrate2 = None
        self.direction1 = None
        self.direction2 = None

        """Query model and version number of firmware to check pump is
        OK. Responds with a load of stuff, but the last three characters
        are XXY, where XX is the address and Y is pump status. :, > or <
        when stopped, running forwards, or running backwards. Confirm
        that the address is correct. This acts as a check to see that
        the pump is connected and working."""
        try:
            self.write("VER")
            resp = self.read(11)

            if int(resp[-3:-1]) != int(self.address):
                raise PumpError("No response from pump at address %s" % self.address)
            print("%s: %s " % (self.name, resp[1:-4]))

        except PumpError:
            self.serial.close()
            raise

        logging.info(
            "%s: created at address %s on %s", self.name, self.address, self.serial.port,
        )

    def __repr__(self):
        string = ""
        for attr in self.__dict__:
            string += "%s: %s\n" % (attr, self.__dict__[attr])
        return string

    def write(self, command):
        self.serial.write(bytes(self.address + command + "\r", encoding="ascii"))

    def read(self, num_bytes=5):
        response = self.serial.read(num_bytes)

        if len(response) == 0:
            raise PumpError("%s: no response to command" % self.name)
        return response.decode().rstrip()

    def setmode(self, mode):
        """Set pump mode.

        Pump 33 has 3 mode: Auto Stop, Proportional, and Continuous.
        The Command for them are AUT, PRO, and CON, respectively.
        """
        # Check if the input is a valid mode
        if mode not in ["AUT", "PRO", "CON"]:
            raise PumpError("%s: %s is not a mode name" % (self.name, mode))

        self.write("MOD" + mode)
        resp = self.read(5)

        if resp[-1] == ":" or resp[-1] == "<" or resp[-1] == ">":
            # check if mode has been set correctlry
            self.write("MOD")
            resp = self.read(15)

            returned_mode = resp[1:-4]

            # Check mode was set accurately
            if returned_mode != mode:
                logging.error(
                    "%s: set mode (%s) does not match mode" " returned by pump (%s)",
                    self.name,
                    mode,
                    returned_mode,
                )
            elif returned_mode == mode:
                self.mode = mode
                print("%s: mode set to %s" % (self.name, self.mode))
                logging.info("%s: mode set to %s", self.name, self.mode)
        else:
            raise PumpError("%s: unknown response to setmode" % self.name)

    def setdiameter1(self, diameter):
        """Set syringe 1 diameter (millimetres).

        Pump 33 syringe diameter range is 0.1-50 mm. Note that the diameters
        are in the following format: ffffff (e.g. 44.755 or 0.3257)
        """
        if diameter > 50 or diameter < 0.1:
            raise PumpError(
                "%s: diameter %s mm is out of range" % (self.name, str(diameter))
            )

        # TODO: Got to be a better way of doing this with string formatting
        diameter = str(diameter)

        # Pump only considers float format: ffffff
        if len(diameter) > 6:
            diameter = diameter[0:6]
            diameter = remove_crud(diameter)
            logging.warning("%s: diameter truncated to %s mm", self.name, diameter)
        else:
            diameter = remove_crud(diameter)

        # Send command
        self.write("DIA A" + diameter)
        resp = self.read(5)

        # Pump replies with address and status (:, < or >)
        if resp[-1] == ":" or resp[-1] == "<" or resp[-1] == ">":
            # check if diameter has been set correctlry
            self.write("DIA A")
            resp = self.read(15)

            returned_diameter = resp[1:-4]

            # Check diameter was set accurately
            if float(returned_diameter) != float(diameter):
                logging.error(
                    "%s: set diameter (%s mm) does not match diameter"
                    "returned by pump (%s mm)",
                    self.name,
                    diameter,
                    returned_diameter,
                )
            elif float(returned_diameter) == float(diameter):
                self.diameter1 = float(returned_diameter)
                print("%s: syringe 1 diameter set to %s mm" % (self.name, self.diameter1))
                logging.info(
                    "%s: syringe 1 diameter set to %s mm", self.name, self.diameter1
                )
        else:
            raise PumpError("%s: unknown response to setdiameter1" % self.name)

    def setdiameter2(self, diameter):
        """Set syringe 2 diameter (millimetres) (only valid in Proportional
        (PRO) mode).

        Pump 33 syringe diameter range is 0.1-50 mm. Note that the diameters
        are in the following format: ffffff (e.g. 44.755 or 0.3257)
        """
        # Check if the pump is in Proportional mode
        self.write("MOD")
        resp = self.read(15)

        returned_mode = resp[1:-4]

        if returned_mode != "PRO":
            raise PumpError(
                "%s: set syringe 2 diameter is only valid in "
                "Proportional mode" % self.name
            )

        if diameter > 50 or diameter < 0.1:
            raise PumpError(
                "%s: diameter %s mm is out of range" % (self.name, str(diameter))
            )

        # TODO: Got to be a better way of doing this with string formatting
        diameter = str(diameter)

        # Pump only considers 2 d.p. - anymore are ignored
        if len(diameter) > 6:
            diameter = diameter[0:6]
            diameter = remove_crud(diameter)
            logging.warning("%s: diameter truncated to %s mm", self.name, diameter)
        else:
            diameter = remove_crud(diameter)

        # Send command
        self.write("DIA B" + diameter)
        resp = self.read(5)

        # Pump replies with address and status (:, < or >)
        if resp[-1] == ":" or resp[-1] == "<" or resp[-1] == ">":
            # check if diameter has been set correctlry
            self.write("DIA B")
            resp = self.read(15)

            returned_diameter = resp[1:-4]

            # Check diameter was set accurately
            if float(returned_diameter) != float(diameter):
                logging.error(
                    "%s: set diameter (%s mm) does not match diameter"
                    " returned by pump (%s mm)",
                    self.name,
                    diameter,
                    returned_diameter,
                )
            elif float(returned_diameter) == float(diameter):
                self.diameter2 = float(returned_diameter)
                print("%s: syringe 2 diameter set to %s mm" % (self.name, self.diameter2))
                logging.info(
                    "%s: syringe 2 diameter set to %s mm", self.name, self.diameter2
                )
        else:
            raise PumpError("%s: unknown response to setdiameter2" % self.name)

    def setflowrate1(self, flowrate):
        """Set both syringe 1 and 2 to the same flow rate (microlitres per
        minute) in Auto Stop and Continuous mode. In Proportional mode, it
        only sets flow rate for syringe 1.

        Flow rate is converted to a string. Pump 33 requires it to have
        the format: ffffff.

        The pump will tell you if the specified flow rate is out of
        range. This depends on the syringe diameter. See Pump 33 manual.
        """
        flowrate = str(flowrate)

        if len(flowrate) > 6:
            flowrate = flowrate[0:6]
            flowrate = remove_crud(flowrate)
            logging.warning("%s: flow rate truncated to %s uL/min", self.name, flowrate)
        else:
            flowrate = remove_crud(flowrate)

        self.write("RAT A" + flowrate + "UM")
        resp = self.read(5)

        if resp[-1] == ":" or resp[-1] == "<" or resp[-1] == ">":
            # Flow rate was sent, check it was set correctly
            self.write("RAT A")
            resp = self.read(20)

            returned_flowrate = remove_crud(resp[1:7])

            if float(returned_flowrate) != float(flowrate):
                logging.error(
                    "%s: set flowrate (%s uL/min) does not match"
                    "flowrate returned by pump (%s uL/min)",
                    self.name,
                    flowrate,
                    returned_flowrate,
                )
            elif float(returned_flowrate) == float(flowrate):
                self.flowrate1 = float(returned_flowrate)
                print(
                    "%s: syringe 1 flow rate set to %s uL/min"
                    % (self.name, str(self.flowrate1))
                )
                logging.info(
                    "%s: syringe 1 flow rate set to %s uL/min",
                    self.name,
                    str(self.flowrate1),
                )
        elif "OOR" in resp:
            raise PumpError(
                "%s: flow rate (%s uL/min) is out of range" % (self.name, flowrate)
            )
        else:
            raise PumpError("%s: unknown response to setflowrate1" % self.name)

    def setflowrate2(self, flowrate):
        """Set syringe 2 flow rate (microlitres per minute)(only valid
        in Proportional (PRO) mode).

        Flow rate is converted to a string. Pump 33 requires it to have
        the format: ffffff.

        The pump will tell you if the specified flow rate is out of
        range. This depends on the syringe diameter. See Pump 33 manual.
        """
        # Check if the pump is in Proportional mode
        self.write("MOD")
        resp = self.read(15)

        returned_mode = resp[1:-4]

        if returned_mode != "PRO":
            raise PumpError(
                "%s: set syringe 2 flow rate is only valid "
                "in Proportional mode" % self.name
            )

        flowrate = str(flowrate)

        if len(flowrate) > 6:
            flowrate = flowrate[0:6]
            flowrate = remove_crud(flowrate)
            logging.warning("%s: flow rate truncated to %s uL/min", self.name, flowrate)
        else:
            flowrate = remove_crud(flowrate)

        self.write("RAT B" + flowrate + "UM")
        resp = self.read(5)

        if resp[-1] == ":" or resp[-1] == "<" or resp[-1] == ">":
            # Flow rate was sent, check it was set correctly
            self.write("RAT B")
            resp = self.read(20)

            returned_flowrate = remove_crud(resp[1:7])

            if float(returned_flowrate) != float(flowrate):
                logging.error(
                    "%s: set flowrate (%s uL/min) does not match"
                    "flowrate returned by pump (%s uL/min)",
                    self.name,
                    flowrate,
                    returned_flowrate,
                )
            elif float(returned_flowrate) == float(flowrate):
                self.flowrate2 = float(returned_flowrate)
                print(
                    "%s: syringe 2 flow rate set to %s uL/min"
                    % (self.name, str(self.flowrate2))
                )
                logging.info(
                    "%s: syringe 2 flow rate set to %s uL/min",
                    self.name,
                    str(self.flowrate2),
                )
        elif "OOR" in resp:
            raise PumpError(
                "%s: flow rate (%s uL/min) is out of range" % (self.name, flowrate)
            )
        else:
            raise PumpError("%s: unknown response to setflowrate2" % self.name)

    def setdirection1(self, direction):
        """Set syringe 1 direction.

        Pump 33 has 3 direction settings: INF (infusion), REF (refill), REV
        (reverse).
        """
        # Check if the input is a valid direction command
        if direction not in ["INF", "REF", "REV"]:
            raise PumpError("%s: %s is not a direction name" % (self.name, direction))

        self.write("DIR")
        resp = self.read(15)
        original_dir = resp[1:4]

        if original_dir == direction:
            pass
        else:
            # this would change directions of both syringes
            self.write("DIR REV")

            resp = self.read(5)
            # change syringe 2 direction back
            self.par()

        if resp[-1] == ":" or resp[-1] == "<" or resp[-1] == ">":
            # check if direction has been set correctlry
            self.write("DIR")
            resp = self.read(15)

            returned_direction = resp[1:4]

            if returned_direction != direction:
                logging.error(
                    "%s: set syringe 1 direction (%s) does not match"
                    " direction returned by pump (%s)",
                    self.name,
                    direction,
                    returned_direction,
                )
            elif returned_direction == direction:
                self.direction1 = direction
                print("%s: syringe 1 direction set to %s" % (self.name, self.direction1))
                logging.info(
                    "%s: syringe 1 direction set to %s", self.name, self.direction1
                )
        else:
            raise PumpError("%s: unknown response to setdirection1" % self.name)

    def setdirection2(self, direction):
        """Set syringe 2 direction.

        Pump 33 has 3 direction setting: INF (infusion), REF (refill), REV
        (reverse).
        """
        # Check if the input is a valid direction command
        if direction not in ["INF", "REF", "REV"]:
            raise PumpError("%s: %s is not a direction name" % (self.name, direction))

        self.write("DIR")
        resp = self.read(15)
        original_dir1 = resp[1:4]

        self.write("PAR")
        resp = self.read(15)
        original_par = resp[1:-4]

        # set syringe 2 direction
        if original_dir1 == direction and original_par == "ON":
            pass
        elif original_dir1 != direction and original_par == "OFF":
            pass
        else:
            self.par()

        self.write("PAR")
        resp = self.read(15)

        returned_par = resp[1:-4]

        if resp[-1] == ":" or resp[-1] == "<" or resp[-1] == ">":
            # check if direction has been set correctlry
            self.write("DIR")
            resp = self.read(15)

            returned_direction = resp[1:4]

            if returned_direction != direction and returned_par == "ON":
                logging.error(
                    "%s: set syringe 2 direction (%s) does not match"
                    " direction returned by pump (%s)",
                    self.name,
                    direction,
                    returned_direction,
                )
            elif returned_direction == direction and returned_par == "OFF":
                logging.error(
                    "%s: set syringe 2 direction (%s) does not match"
                    " direction returned by pump (%s)",
                    self.name,
                    direction,
                    returned_direction,
                )
            else:
                self.direction2 = direction
                print("%s: syringe 2 direction set to %s" % (self.name, self.direction2))
                logging.info(
                    "%s: syringe 2 direction set to %s", self.name, self.direction2
                )
        else:
            raise PumpError("%s: unknown response to setdirection2" % self.name)

    def start(self):
        """Start the pump"""
        self.write("RUN")

        resp = self.read(5)

        if resp[-1] != ">" and resp[-1] != "<":
            raise PumpError("%s: unknown response to start" % self.name)
        logging.info("%s: started", self.name)

    def stop(self):
        """Stop the pump"""
        self.write("STP")

        resp = self.read(5)

        if resp[-1] != ":":
            raise PumpError("%s: unexpected response to stop" % self.name)
        logging.info("%s: stopped", self.name)

    def par(self):
        """Switch the pump between parallel and reciprocal pumping
        direction.

        ON = Parallel (syringes in the same direction)
        OFF = Reciprocal (syringes in the opposite direction)
        """
        self.write("PAR")
        resp = self.read(15)
        original_par = resp[1:-4]

        if original_par == "ON":
            self.write("PAR OFF")
            resp = self.read(5)

            if resp[-1] == ":" or resp[-1] == "<" or resp[-1] == ">":
                # check if Parallel/Reciprocal has been set correctlry
                self.write("PAR")
                resp = self.read(15)
                returned_par = resp[1:-4]
                if returned_par != "OFF":
                    logging.error(
                        "%s: set Parallel/Reciprocal (%s) does not work",
                        self.name,
                        self.name,
                    )
                elif returned_par == "OFF":
                    logging.info("%s: switch from Parallel to Reciprocal", self.name)
        elif original_par == "OFF":
            self.write("PAR ON")
            resp = self.read(5)

            if resp[-1] == ":" or resp[-1] == "<" or resp[-1] == ">":
                # check if Parallel/Reciprocal has been set correctlry
                self.write("PAR")
                resp = self.read(15)
                returned_par = resp[1:-4]
                if returned_par != "ON":
                    logging.error(
                        "%s: set Parallel/Reciprocal (%s) does not work",
                        self.name,
                        self.name,
                    )
                elif returned_par == "ON":
                    logging.info("%s: switch from Reciprocal to Parallel", self.name)
        else:
            raise PumpError("%s: unknown response to setdirection" % self.name)


class PumpError(Exception):
    pass


# Command line options
# Run with -h flag to see help

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Command line interface to "
        "pumpy module for control of Harvard Pump "
        "11 (default) or PHD2000 syringe pumps, or"
        " SSI Mighty Mini Pump"
    )
    parser.add_argument("port", help="serial port")
    parser.add_argument(
        "address", help="pump address (Harvard pumps)", type=int, nargs="?", default=0
    )
    parser.add_argument("-d", dest="diameter", help="set syringe diameter", type=int)
    parser.add_argument("-f", dest="flowrate", help="set flow rate")
    parser.add_argument("-t", dest="targetvolume", help="set target volume")
    parser.add_argument(
        "-w",
        dest="wait",
        help="wait for target volume to be" " reached; use with -infuse or -withdraw",
        action="store_true",
    )

    # TODO: only allow -w if infuse, withdraw or stop have been specified
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-infuse", action="store_true")
    group.add_argument("-withdraw", action="store_true")
    group.add_argument("-stop", action="store_true")

    pumpgroup = parser.add_mutually_exclusive_group()
    pumpgroup.add_argument("-PHD2000", help="To control PHD2000", action="store_true")
    pumpgroup.add_argument(
        "-MightyMini", help="To control Mighty Mini", action="store_true"
    )
    args = parser.parse_args()

    if args.MightyMini:
        pimp_chain = Chain(args.port, stopbits=serial.STOPBITS_ONE)
    else:
        pimp_chain = Chain(args.port)

    # Command precedence:
    # 1. stop
    # 2. set diameter
    # 3. set flow rate
    # 4. set target
    # 5. infuse|withdraw (+ wait for target volume)

    try:
        if args.PHD2000:
            pump = PHD2000(pimp_chain, args.address, name="PHD2000")
        elif args.MightyMini:
            pump = MightyMini(pimp_chain, name="MightyMini")
        else:
            pump = Pump(pimp_chain, args.address, name="11")

        if args.stop:
            pump.stop()

        if args.diameter:
            pump.setdiameter(args.diameter)

        if args.flowrate:
            pump.setflowrate(args.flowrate)

        if args.targetvolume:
            pump.settargetvolume(args.targetvolume)

        if args.infuse:
            pump.infuse()
            if args.wait:
                pump.waituntiltarget()

        if args.withdraw:
            pump.withdraw()
            if args.wait:
                pump.waituntiltarget()
    finally:
        pimp_chain.close()
