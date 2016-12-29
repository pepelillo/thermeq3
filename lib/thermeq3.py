import t3_var
import maxeq3
import time
import os
import datetime
import bridge
import logmsg
import weather
import public_ip
import mailer
import ast
import profiles
import autoupdate
import secweb
import csvfile
import sys
import dummy


# TBI RPI
# import action

t3 = None


class Thermeq3Object(object):
    def __init__(self):
        self.eq3 = None
        self.setup = t3_var.Thermeq3Setup()
        self.var = t3_var.Thermeq3Variables()
        self.status = t3_var.Thermeq3Status()
        self.secweb = secweb.SecWeb()
        self.err_str = ""

    def __repr__(self):
        return str(self.__class__) + ": " + str(self.__dict__)

    def __str__(self):
        return str(self.__class__) + ": " + str(self.__dict__)

    def get_target(self):
        return self.setup.target

    def check_target(self):
        if self.is_target_yun() and os.name == "posix":
            return True
        else:
            return False

    def is_target_yun(self):
        if self.get_target() == "yun":
            return True
        else:
            return False

    def prepare(self):
        self.eq3 = maxeq3.EQ3Data(self.setup.max_ip, 62910)
        self.setup.init_intervals()
        logmsg.start(self.setup.log_filename)
        self.status.update('s')

        # check if bridge file is in /root, from V230 bridge file is on sd card
        if os.path.exists("/root/" + self.setup.devname + ".bridge"):
            result = 0
            try:
                result = os.system("mv /root/" + self.setup.devname + ".bridge " + self.setup.place + self.setup.devname + ".bridge")
            except TypeError:
                pass
            if not result >> 8 == 0:
                logmsg.update("Error " + str(result >> 8) + " during moving bridge file.", 'E')

        br_data = bridge.load(self.setup.bridge_file)
        self._literal_process(br_data)

        # re-initialize variables
        self.get_control_values()

        self.queue_msg("S")
        self.get_ip()

        self.eq3.read_data(True)
        self.export_csv("init")

        self.status.update('i')
        self.update_all()
        self.secweb.start(self.setup.place)

    def _set_status(self):
        tmp_status = ""
        if self.var.heating:
            tmp_status += 'h'
        else:
            tmp_status += 'i'
        if self.var.ventilating:
            tmp_status += 'v'
        self.status.update(tmp_status)

    def queue_msg(self, msg):
        logmsg.update("Queueing [" + str(msg) + "]", 'D')
        self.var.msgQ.insert(0, msg)

    @staticmethod
    def _is_beta():
        return bridge.try_read("beta", False).upper() == "YES"

    def process_msg(self):
        """
        Process message queue
        :return: nothing
        """
        while len(self.var.msgQ) > 0:
            logmsg.update("Message queue: " + str(self.var.msgQ), 'D')
            wait_cycle = False
            if self.is_target_yun():
                # this is waiting routine to ensure that msg queue is processed by 32u4 part
                tmp_br = bridge.get("msg")
                logmsg.debug("bridge:", tmp_br)
                if not tmp_br == "":
                    time.sleep(self.setup.timeout)
                    wait_cycle = True
            if not wait_cycle:
                to_send = self.var.msgQ.pop()
                logmsg.update("Sending message [" + str(to_send) + "]", 'D')
                if to_send == "R":
                    bridge.save(self.setup.bridge_file)
                if self.is_target_yun():
                    # signal to 32u4 that we have something to process
                    bridge.put("msg", to_send)
                elif self.get_target() == "rpi":
                    if to_send == "H":
                        # TBI RPI
                        # uncomment line below for RPi
                        # action.do(True)
                        pass
                    elif to_send == "S":
                        # TBI RPI
                        # uncomment line below for RPi
                        # action.do(False)
                        pass
                elif os.name == "nt":
                    # insert windows code here
                    # print "nt"
                    pass

    def export_csv(self, cmd="init"):
        if cmd == "init":
            csvfile.init(self.setup.place, self.setup.devname)
            csvfile.write("Date,heating,ventilating,")
            csvfile.write(self.eq3.headers())
            csvfile.write("\n")
        elif cmd == "close":
            csvfile.close()

    def get_control_values(self):
        """ read control values from bridge """
        # try read preference settings, total or per
        self.setup.preference = bridge.try_read("pref")
        # try read % valve for heat command
        self.setup.valve_switch = bridge.try_read("valve")
        self.setup.svpnmw = bridge.try_read("svpnmw")
        self.setup.total_switch = bridge.try_read("total")
        # try get readMAX interval value, if not set it
        self.setup.intervals["max"][0] = bridge.try_read("int")
        self.setup.intervals["slp"][0] = self.setup.intervals["max"][0] / self.setup.intervals["slp"][1]
        # try read num of valves to turn heat on
        self.setup.valve_num = bridge.try_read("valves")
        # try read if auto update is OK
        self.setup.au = bridge.try_read("au")
        # try read how many minutes you can ignore valve after closing window
        self.eq3.ignore_time = bridge.try_read("ign_op")
        # and if open windows warning is disabled, 0 = enables, 1 = disabled
        self.setup.no_oww = bridge.try_read("no_oww")

    def set_control_values(self):
        """ set control values to bridge """
        bridge.put("pref", self.setup.preference)
        bridge.put("valve", self.setup.valve_switch)
        bridge.put("svpnmw", self.setup.svpnmw)
        bridge.put("total", self.setup.total_switch)
        bridge.put("int", self.setup.intervals["max"][0])
        bridge.put("valves", self.setup.valve_num)
        bridge.put("au", self.setup.au)
        bridge.put("ign_op", self.eq3.ignore_time)
        bridge.put("no_oww", self.setup.no_oww)
        bridge.put("ht", self.var.ht)
        bridge.put("ign", self.eq3.ignored_valves)

    # check if its right time to update
    def isrt(self, selector):
        tm = time.time()
        if tm > self.setup.intervals[selector][2]:
            self.setup.intervals[selector][2] = tm + self.setup.intervals[selector][0]
            return True
        else:
            return False

    def _is_win_open_too_long(self, key):
        """
        Returns true if window is open longer that defined (setup.intervals["oww"][0]) time
        :param key: key
        :return: boolean
        """
        """ return True if window open time is > defined warning interval """
        v = self.eq3.devices[key]
        if self.eq3.is_window_open(key):
            tmp = (datetime.datetime.now() - v[5]).total_seconds()
            if tmp > self.setup.intervals["oww"][0]:
                return True
            else:
                return False

    def _do_heat(self, state):
        if state:
            bridge.put("ht", self.var.ht)
            self.queue_msg("H")
        else:
            self.queue_msg("S")
        self.var.heating = state
        self.update_counters(state)
        self._set_status()

    def heat_or_not_room(self):
        """
        choose to heat or not to heat, consider valve averages in room, not single valves
        e.g. if room have 2 valves, both of them must be over valve_pos
        :return: list, [heat value, total heat valve dictionary]
        """
        h = 0
        t = 0
        hv = {}
        for k, v in self.eq3.rooms.iteritems():
            if self.setup.preference == "per":
                # and valve position is over single valve position no matter what
                if v[4] > self.setup.svpnmw:
                    h = 3
                # or valve is over desired position to switch heating on
                if v[4] > self.setup.valve_switch:
                    hv.update({k: [v[0], v[0], v[0]]})
                    if len(hv) >= self.setup.valve_num:
                        h = 1
            elif self.setup.preference == "total":
                t += v[0]
                if t >= self.setup.total_switch:
                    h = 2
        return [h, t, hv]

    def heat_or_not(self):
        """
        choose to heat or not to heat
        :return: list, [heat value, total heat valve dictionary]
        """
        h = 0
        t = 0
        hv = {}
        for k, v in self.eq3.valves.iteritems():
            # if valve is ok to evaluate
            if self.eq3.count_valve(k):
                # if preference is per valve
                if self.setup.preference == "per":
                    # and valve position is over single valve position no matter what
                    if v[0] > self.setup.svpnmw:
                        h = 3
                    # or valve is over desired position to switch heating on
                    if v[0] > self.setup.valve_switch:
                        hv.update(self.eq3.get_key_full_name(k))
                        if len(hv) >= self.setup.valve_num:
                            h = 1
                elif self.setup.preference == "total":
                    t += v[0]
                    if t >= self.setup.total_switch:
                        h = 2
        return [h, t, hv]

    def get_open_windows(self, debug=False):
        """
        get dictionary of open windows
        :param debug, boolean
        :return: dictionary
        """
        ow = {}
        for k, v in self.eq3.devices.iteritems():
            if v[4] == 2:
                room_id = str(v[3])
                room_name = str(self.eq3.rooms[room_id][0])
                if self._is_win_open_too_long(k):
                    ow.update({k: room_name})
                    if debug:
                        logmsg.update("Warning condition for window " + str(k) + " met")
        return ow

    def process_device_errors(self):
        """
        Checks for error on devices
        :return: nothing
        """
        for k, v in self.eq3.devices.iteritems():
            if self.eq3.is_battery_error(k):
                self.send_warning("battery", k, "")
            if self.eq3.is_radio_error(k):
                self.send_warning("error", k, "")

    def process_windows(self, ow):
        """
        Process dictionary and set ventilation status, then update owl web file
        :param ow: dictionary, open windows
        :return: nothing
        """
        # if count of open windows >= number of windows, that mean ventilation
        if len(ow) >= self.setup.ventilate_num:
            self.var.ventilating = True
        else:
            self.var.ventilating = False
        self.secweb.write("owl", ow)
        # if open window warning is on and open window(s) count < number of open windows, that mean ventilation
        # send warnings for these windows
        if not self.setup.no_oww and len(ow) < self.setup.ventilate_num:
            for k, v in ow.iteritems():
                self.send_warning("window", k, "")
                pass

    def get_heat_string(self, heat, heat_valve, grt):
        """
        Prepare string for log
        :param heat: boolean
        :param heat_valve: dictionary
        :param grt: int
        :return: string
        """
        valve_count = len(heat_valve)
        txt = "heating due "
        if heat == 1 and valve_count >= self.setup.valve_num:
            txt += str(valve_count) + " valve(s)"
            for k, v in heat_valve.iteritems():
                txt += ", room " + str(v[0]) + ", valve " + str(v[1]) + "@" + str(v[2][0])
        elif heat == 2:
            txt += "sum of valves = " + str(grt)
        elif heat == 3:
            txt += "single valve position, no matter what " + str(self.setup.svpnmw) + "%"
        return txt

    def control(self):
        self.check_var()
        open_windows = self.get_open_windows(True)

        # process open windows
        self.process_windows(open_windows)

        # and now showtime
        # heat: 0 = disable, 1 = heat per, 2 = total, 3 = svpnmw
        # heat, grt, heat_valve = self.heat_or_not_room()
        heat, grt, heat_valve = self.heat_or_not()

        # increment number of readings with heat on
        if bool(heat):
            self.var.heat_readings += 1

        if bool(heat) != self.var.heating:
            mode = bridge.try_read("mode").upper() == "AUTO"
            logmsg.debug("mode=", mode,
                         ", heat=", heat,
                         ", bool(heat)=", bool(heat))
            if heat > 0:
                logmsg.update(self.get_heat_string(heat, heat_valve, grt), 'I')
            else:
                logmsg.update("heating stopped.", 'I')
            if mode:
                self._do_heat(bool(heat))

    def intervals(self):
        # do upgrade according schedule
        if self.isrt("upg"):
            self._do_autoupdate()
        # do update variables according schedule
        if self.isrt("var"):
            logmsg.update("Updating variables...")
            self.update_all()
            bridge.save(self.setup.bridge_file)
            self.get_ip()
            self.update_ignores_2sit()
            self.set_control_values()
        # check max according schedule
        if self.isrt("max"):
            self._do_beta()
            if self._process_command():
                return 0xFF
            self._do_thermeq()
        self.process_msg()

    def _process_command(self):
        """
        Process command from cmd bridge
        :return: nothing
        """
        cmd = bridge.get_cmd()
        if cmd == "quit":
            return True
        elif cmd == "log_debug":
            logmsg.level('D')
        elif cmd == "log_info":
            logmsg.level('I')
        elif cmd[0:4] == "mute":
            self._mute(cmd[4:])
        elif cmd == "rebridge":
            br_data = bridge.load(self.setup.bridge_file)
            self._literal_process(br_data)
            self.update_all()
        elif cmd == "led":
            if self.var.heating:
                self.queue_msg("H")
            else:
                self.queue_msg("S")
        elif cmd == "upgrade":
            self._do_autoupdate()
        # add dummy room, valve, window, for testing, format: add:o = add open window, add:c = add closed window
        # add:r = remove
        elif cmd[0:4] == "dmy:":
            if cmd[4:5] == 'o':
                dummy.add_dummy(True)
            elif cmd[4:5] == 'c':
                dummy.add_dummy(False)
            elif cmd[4:5] == 'r':
                dummy.remove_dummy()

    def _do_thermeq(self):
        """
        Do what thermeq must do
        :return: nothing
        """
        # save open window dictionary for adjustment
        ow = self.get_open_windows()
        eq3_result, eq3_error = self.eq3.read_data(False)
        if eq3_result:
            # set values
            self.get_control_values()
            # log messages
            logmsg.update(self._status_msg() + " Checking #" + str(self.setup.intervals["max"][0]) + " sec", 'I')
            logmsg.update(self.eq3.plain(), 'I')
            # do control
            self.control()
            # update JSONs
            self.write_strings()
            # set status
            self._set_status()
            # do logging
            self.do_device_logging()
            # do some valve_ignored adjustment
            self.adjust_ignored_valves(ow)
            # save bridge
            self.set_control_values()
            bridge.save(self.setup.bridge_file)
        else:
            self.var.heating = None
            self.queue_msg('E')
            # flush error to log
            for k in eq3_error:
                logmsg.update(k, 'E')

    def _do_autoupdate(self):
        """
        Perform autoupdate
        :return: nothing
        """
        if autoupdate.do(self.setup.version, self._is_beta()):
            logmsg.update("thermeq3 updated.")
            temp_key = self.eq3.maxid["sn"]
            body = """<h1>Device upgrade information.</h1>
            <p>Hello, I'm your thermostat and I have a information for you.<br/>
            Please take a note, that I found new version of me and I'll be upgraded in few seconds.</br>
            Resistance is futile :).<br/>"""
            self.send_warning("upgrade", temp_key, body)
            self.queue_msg('R')

    def _do_profiles(self):
        """
        Select profile according to selected profile mode
        :return: nothing
        """
        if not self.setup.selected_mode == "NORMAL":
            sm, am, kk = profiles.do(self.setup.selected_mode, self.var.act_mode_idx, self.var.situation)
            if sm != self.setup.selected_mode or am != self.var.act_mode_idx:
                self.setup.selected_mode = sm
                self.var.act_mode_idx = am
                self.set_mode(kk)
                self.set_control_values()

    def _do_beta(self):
        """
        Run beta functionality
        :return: nothing
        """
        # if it's no beta function, please move lines to _do_thermeq
        if self._is_beta():
            self._do_profiles()

    def _mute(self, key):
        """
        Mute warning for windows key
        :param key: string
        :return: nothing
        """
        if key in self.eq3.windows:
            self.eq3.windows[key][0] = datetime.datetime.now()
            self.eq3.windows[key][1] = True
            logmsg.update("OWW for key " + str(key) + " is muted for " + str(
                self.setup.intervals["oww"][2]) + " seconds.")

    def adjust_ignored_valves(self, ow):
        """
        Adjust ignored valves, if window associated with ignored valve was opened too long, extend valve ignore time
        :param ow: dictionary, open windows
        :return: nothing
        """
        ow_now = self.get_open_windows()
        if not cmp(ow_now, ow) == 0:
            adj_list = set(ow) ^ set(ow_now)
            for a in adj_list:
                room = self.eq3.devices[a][3]
                for k, v in self.eq3.devices.iteritems():
                    # this is heating thermostat and is in room where we want ignore all heating thermostats
                    if v[0] == 1 and v[3] == room and k in self.eq3.ignored_valves:
                        tmp_time = self.eq3.ignored_valves[k]
                        self.eq3.ignored_valves.update({k: tmp_time + (1 * self.eq3.ignore_time) * 60})
                        logmsg.update("Valve: " + str(k) + " adjusted.")

    def _status_msg(self):
        """
        Return formatted status string
        :return: string
        """
        logstr = self.status.actual + ", "
        if self.setup.preference == "per":
            logstr += str(self.setup.valve_switch) + "%" + " at " + str(self.setup.valve_num) + " valve(s)."
        elif self.setup.preference == "total":
            logstr += "total value of " + str(self.setup.total_switch) + "."
        return logstr

    @staticmethod
    def _get_uptime():
        if os.name != "nt":
            with open("/proc/uptime", "r") as f:
                uptime_seconds = float(f.readline().split()[0])
                return str(datetime.timedelta(seconds=uptime_seconds)).split(".")[0]
        else:
            uptime = os.popen('systeminfo', 'r')
            # obfuscate warning
            data = uptime.readlines()
            data += ""
            uptime.close()
            return str(0)

    def update_uptime(self):
        tmp = time.time()
        bridge.put("uptime", self._get_uptime())
        bridge.put("appuptime", datetime.timedelta(seconds=int(tmp - self.var.appStartTime)))

    def update_all(self):
        self.update_uptime()
        self.update_counters(False)

    def update_counters(self, heat_start):
        # save the date
        nw = datetime.datetime.date(datetime.datetime.now()).strftime("%d-%m-%Y")
        tm = time.time()

        # is there a key for today?
        logmsg.debug("ht=", self.var.ht, ", heat_start=", heat_start)
        if nw in self.var.ht:
            if heat_start:
                self.var.ht[nw][1] = tm
            elif self.var.heating:
                daily_heat_sum = int(self.var.ht[nw][0] + (tm - self.var.ht[nw][1]))
                logmsg.debug("Daily heat sum=", daily_heat_sum)
                self.var.ht[nw] = [daily_heat_sum, time.time()]
                bridge.put("ht", self.var.ht)
                logmsg.update("Daily heat counter updated to " + str(datetime.timedelta(seconds=daily_heat_sum)))
        else:
            if len(self.var.ht) > 0:
                # if there a key, this must be old key(s)
                # save the old date, and flush values into log
                for k in self.var.ht.keys():
                    v = self.var.ht[k]
                    logmsg.update(str(k) + " daily heating summary: " + str(datetime.timedelta(seconds=v[0])), 'I')
                    logmsg.update("Deleting old daily key: " + str(k), 'D')
                    del self.var.ht[k]
                self.export_csv("close")

            # create the new key
            self.var.ht.update({nw: [0, time.time()]})
            logmsg.update("Creating new daily key: " + str(nw) + "=" + str(self.var.ht[nw]))
            # so its a new day, update other values
            self.export_csv("init")
            # day readings warning, take number of heated readings and divide by 2
            drw = self.var.heat_readings / 2
            logmsg.update("Day reading warnings value=" + str(drw))
            for k, v in self.var.dev_log.iteritems():
                logmsg.update("Valve: " + str(k) + " has value " + str(v[0]))
                if v[0] > drw:
                    logmsg.update("Valve: " + str(k) +
                                  " reports during heating too many same % positions, e.g. " +
                                  str(v[0]) + " per " + str(drw))
                self.var.dev_log[k][0] = 0
            self.var.heat_readings = 0
            bridge.save(self.setup.bridge_file)
        bridge.put("ht", self.var.ht)

    def _literal_process(self, bridge_data, obj_prefix="self."):
        """
        Process bridge string, update variables as typed in bridge.cw dictionary
        :param bridge_data: dictionary
        :param obj_prefix: string
        :return: nothing
        """
        try:
            for k, v in bridge_data.iteritems():
                if k in bridge.pcw:
                    pcw_v = bridge.pcw[k]
                    if pcw_v[1]:
                        d = pcw_v[2].split(".")
                        default = pcw_v[0]
                        cwt = type(default)
                        obj = obj_prefix + d[0]
                        name = d[1]
                        if cwt is str:
                            value = str(v)
                        elif cwt is int:
                            value = int(v)
                        elif cwt is bool:
                            value = bool(v)
                        elif cwt is float:
                            value = float(v)
                        elif cwt is dict:
                            value = ast.literal_eval(v)
                        else:
                            value = v
                        try:
                            obj_obj = eval(obj)
                        except:
                            logmsg.update("Error evaluating object: " + str(obj), 'E')
                        else:
                            try:
                                setattr(obj_obj, name, value)
                                # logmsg.debug("object:", obj, ", name:", name, " to:", value)
                            except:
                                logmsg.update("Error processing object: " + str(obj) + ", with name: " + str(name), 'E')
        except AttributeError:
            logmsg.update('Bridge file has attribute error. Possibly missing')

    def update_ignores_2sit(self):
        """
        Update open window variables according to weather
        :return: nothing
        """
        self.var.situation = weather.weather_for_woeid(self.setup.location, self.setup.owm_api_key)
        if None not in self.var.situation.viewvalues():
            temp = int(self.var.situation["current_temp"])
            if temp is not None:
                # modify OWW
                # interval for oww 5min to 360min
                tmr = weather.interval_scale(temp, (-35.0, 35.0), (0, 10), (5, 360), True)
                self.setup.intervals["oww"] = [tmr * 60, (3 * tmr) * 60, (3 * tmr) * 60]
                logmsg.update("OWW interval updated to " + str(self.setup.intervals["oww"]))

                # and now modify valve ignore time
                # interval for valve ignore 20min to 120min
                tmr = weather.interval_scale(temp, (0.0, 35.0), (1.7, 3.0), (20, 120), False)
                self.eq3.ignore_time = self.setup.window_ignore_time + tmr
                bridge.put("ign_op", self.eq3.ignore_time)
                logmsg.update("Valve ignore interval updated to " + str(self.eq3.ignore_time), 'D')

    def set_mode(self, value):
        if value[3] == "total":
            self.setup.total_switch = value[2]
        else:
            self.setup.valve_switch = value[2]
        self.setup.preference = value[3]
        self.setup.intervals["max"][0] = value[4]
        self.setup.valve_num = value[5]
        # just sleep value, always calculated as max[0] / slp[1]
        self.setup.intervals["slp"][0] = int(value[4] / self.setup.intervals["slp"][1])
        bridge.put("int", self.setup.intervals["max"][0])

    def get_ip(self):
        """
        Get IP and set variables
        :return: nothing
        """
        tmp = public_ip.get()
        if tmp == 0xFF:
            logmsg.update("Error getting IP address from hostname, please check resolv.conf or hosts or both!", 'E')
        else:
            self.setup.my_ip = tmp
            if public_ip.is_private(tmp):
                log_str = "Local"
            else:
                log_str = "Public"
            logmsg.update(log_str + " IP address: " + self.setup.my_ip)

    # some warnings
    def send_warning(self, selector, dev_key, body_txt):
        subject = ""
        body = ""
        try:
            d = self.eq3.devices[dev_key]
        except KeyError:
            d = [1, "KeyError", "Key Error", 99, 0, datetime.datetime(2016, 01, 01, 12, 00, 00), 18, 56, 7]
            logmsg.update("Key error: " + str(dev_key), 'E')
        dn = d[2]
        r = d[3]
        rn = self.eq3.rooms[str(r)]
        sil = self.silence(dev_key, selector == "window")
        if sil == 1:
            logmsg.update("Warning for device " + str(dev_key) + " is muted!")
            return
        mute_str = "http://" + self.setup.my_ip + ":" + str(self.setup.ext_port) + "/data/put/command/mute" + \
                   str(dev_key)
        if selector == "window":
            dt_now = datetime.datetime.now()
            oww = int((dt_now - self.eq3.windows[dev_key][0]).total_seconds())
            # owd = int((dt_now - self.eq3.devices[dev_key][5]).total_seconds())
            owd = dt_now - self.eq3.devices[dev_key][5]
            if sil == 0 and oww < self.setup.intervals["oww"][1]:
                return
            subject = "Open window in room " + str(rn[0]) + ". Warning from thermeq3 device"
            body = """<h1>Device %(a0)s warning.</h1>
            <p>Hello, I'm your thermostat and I have a warning for you.<br/>
            Please take a care of window <b>%(a0)s</b> in room <b>%(a1)s</b>.
            Window in this room is now opened more than <b>%(a2)s</b>.<br/>
            Threshold for warning is <b>%(a3)d</b> mins.<br/>
            </p><p>You can <a href="%(a4)s">mute this warning</a> for %(a5)s mins.""" % \
                   {'a0': str(dn),
                    'a1': str(rn[0]),
                    # 'a2': int(owd / 60),
                    'a2': str(owd).split('.')[0],
                    # 'a3': int(self.setup.intervals["oww"][0]/ 60),
                    'a3': int((self.setup.intervals["oww"][0] * self.eq3.windows[dev_key][3]) / 60),
                    'a4': str(mute_str),
                    'a5': int(self.setup.intervals["oww"][2] / 60)}
        else:
            if sil == 0 and not self.isrt("wrn"):
                return
            if selector == "battery":
                subject = "Battery status for device " + str(dn) + ". Warning from thermeq3 device"
                body = """<h1>Device %(a0)s battery status warning.</h1>
                <p>Hello, I'm your thermostat and I have a warning for you.<br/>
                Please take a care of device <b>%(a0)s</b> in room <b>%(a1)s</b>.
                This device have low batteries, please replace batteries.<br/>
                </p><p>You can <a href="%(a2)s">mute this warning</a> for %(a3)s mins.""" % \
                       {'a0': str(dn),
                        'a1': str(rn[0]),
                        'a2': str(mute_str),
                        'a3': int(self.setup.intervals["wrn"][1] / 60)}
            elif selector == "error":
                subject = "Error report for device " + str(dn) + ". Warning from thermeq3 device"
                body = """<h1>Device %(a0)s radio error.</h1>
                <p>Hello, I'm your thermostat and I have a warning for you.<br/>
                Please take a care of device <b>%(a0)s</b> in room <b>%(a1)s</b>.
                This device reports error.<br/>
                </p><p>You can <a href="%(a2)s">mute this warning</a> for %(a3)s mins.""" % \
                       {'a0': str(dn),
                        'a1': str(rn[0]),
                        'a2': str(mute_str),
                        'a3': int(self.setup.intervals["wrn"][1] / 60)}
            elif selector == "openmax":
                subject = "Can't connect to MAX! Cube! Warning from thermeq3 device"
                body = body_txt
            elif selector == "upgrade":
                subject = "thermeq3 device is going to be upgraded"
                body = body_txt

        msg = mailer.compose(self.setup, subject, body)

        if mailer.send_email(self.setup, msg.as_string()) and selector == "window":
            # original code
            # self.eq3.windows[dev_key][0] = dt_now
            # dev code
            # extend warning period by incrementing multiplicand * oww time
            self.eq3.windows[dev_key][3] += 1
            self.eq3.windows[dev_key][0] = dt_now + datetime.timedelta(
                seconds=(self.setup.intervals["oww"][0] * self.eq3.windows[dev_key][3]))
            logmsg.update("Multiplicand for " + str(dev_key) + " updated to " + str(self.eq3.windows[dev_key][3]), 'D')

    def silence(self, key, is_win):
        #
        # eq3.windows = key: OW_time(thisnow), is muted by user(False), warning/error count(0), multiplicand
        #
        # is there key in dict?
        dt = datetime.datetime.now()
        if key not in self.eq3.windows:
            # there no key, so its new warning
            logmsg.update("No key " + str(key) + " in windows. Key added.")
            if is_win:
                self.eq3.windows.update({key: [self.eq3.devices[key][5], False, 0, 1]})
            else:
                self.eq3.windows.update({key: [dt, False, 0, 1]})
            return 2
        else:
            # yes, there it is, so check if we are silent, if so exit, otherwise reset mute
            # threshold, send every X, muted for X
            # "oww": [10*60, 30*60, 45*60]
            # threshold, muted for X, time.time()
            # "wrn": [60*60, 60*60, tm]
            if self.eq3.windows[key][1]:
                # yes, we must be silent
                if is_win:
                    tmp = self.eq3.windows[key][0] + datetime.timedelta(seconds=self.setup.intervals["oww"][2])
                else:
                    tmp = self.eq3.windows[key][0] + datetime.timedelta(seconds=self.setup.intervals["wrn"][1])
                if tmp < dt:
                    return 1
                else:
                    # silence is over
                    self.eq3.windows[key][1] = False

        # increment warning counter for this key
        self.eq3.windows[key][2] += 1
        if self.eq3.windows[key][2] > self.setup.abnormalCount:
            logmsg.update(
                "Abnormal #warnings for device [" + str(key) + "], name [" + str(self.eq3.devices[key][2]) + "]")
            self.eq3.windows[key][2] = 0
        return 0

    def write_strings(self):
        """ construct and write CSV data, Log debug string and current status string """
        logstr = bridge.try_read("status", False) + ", "
        if self.setup.preference == "per":
            logstr += str(self.setup.valve_switch) + "%" + " at " + str(self.setup.valve_num) + " valve(s)."
        elif self.setup.preference == "total":
            logstr += "total value of " + str(self.setup.total_switch) + "."

        csvfile.write(time.strftime("%d/%m/%Y %H:%M:%S", time.localtime()),
                      1 if self.var.heating else 0, 1 if self.var.ventilating else 0)

        rooms = {}
        current = {}
        for k, v in self.eq3.rooms.iteritems():
            rooms.update({str(v[0]): ["", v[2], v[4]]})
            current.update({str(v[0]): {}})

        for k, v in self.eq3.valves.iteritems():
            # update rooms string
            room_id = str(self.eq3.get_full_name(k)[0])
            room_str = rooms[room_id][0]
            room_str += "\n\t[" + str(k) + "] " + '{:<20}'.format(str(self.eq3.devices[k][2])) + "@" + \
                        '{:>3}'.format(str(v[0])) + "% @ " + \
                        '{:>4}'.format(str(v[1])) + "'C # " + '{:>4}'.format(str(v[2])) + "'C "
            cv = self.eq3.count_valve(k)
            if cv:
                room_str += "(+)"
            else:
                room_str += "(-)"

            rooms[room_id][0] = room_str
            # comment line below to use current temp
            csvfile.write(v[0], v[1])
            # uncomment line below to use current temp
            # csvfile.write(v[0], v[2])

            current[room_id].update({str(k): [str(self.eq3.devices[k][2]), str(v[0]),
                                              str(v[1]), str(v[2]), str(1 if cv else 0)]})

        csvfile.write("\n")

        logstr = "Actual positions:"
        for k, v in rooms.iteritems():
            logstr += "\nRoom: " + str(k)
            if v[1]:
                logstr += ", open window"
            logstr += ", average: " + str(v[2]) + "%"
            logstr += str(v[0])

        # second web
        # JSON formatted status
        self.secweb.write("status", current)
        # and bridge variable
        bridge.put("sys", current)
        # nice text web
        logstr.replace("\n", "<br/>")
        logstr.replace("\t", "&#9;")
        self.secweb.write("nice", "<html>\n<title>\nStatus</title>\n<body>\n<p><pre>" +
                          logstr + "</pre></p>\n</body>\n</html>")

    def check_var(self):
        """
        Check if variables are set correctly, reports any problem
        :return: nothing
        """
        if self.setup.valve_num > len(self.eq3.valves):
            logmsg.update("You have only " + str(len(self.eq3.valves)) +
                          " valves, but you want to " + str(self.setup.valve_num) +
                          " of them be checked to turn on heating!")
            self.setup.valve_num = len(self.eq3.valves)
        if self.setup.valve_switch > 90:
            logmsg.update("Valve switch position over 90%!")
            self.setup.valve_switch = 90
        if self.setup.svpnmw > 100:
            logmsg.update("Single valve switch position over 100%!")
        # uncomment below if you want auto correct value
        # self.setup.svpnmw = 100
        if self.setup.valve_switch > self.setup.svpnmw:
            logmsg.update("svpnmw (" + str(self.setup.svpnmw) +
                          "%) is less or equal to valve switch setup (" + str(self.setup.valve_switch) + "%)!")
            self.setup.svpnmw = self.setup.valve_switch

    def do_device_logging(self):
        for k, v in self.eq3.valves.iteritems():
            if k in self.eq3.device_log:
                if self.var.heating and self.eq3.is_same(k):
                    self.eq3.device_log[k][0] += 1
                self.eq3.device_log[k][1] = v[0]
            else:
                self.eq3.device_log.update({k: [0, v[0]]})


# global module functions
def redirect_error(on_off):
    global t3
    """
    Turn error redirection on or off
    :param on_off: boolean
    :return: nothing
    """
    if on_off:
        t3.setup.stderr_log = t3.setup.place + t3.setup.devname + "_error.log"
        try:
            t3.var.ferr = open(t3.setup.stderr_log, "a")
        except Exception:
            raise
        else:
            t3.var.original_stderr = sys.stderr
            sys.stderr = t3.var.ferr
            # print >> sys.stderr, time.strftime("%H:%M:%S", time.localtime()), "Redirection active"
    else:
        # print >> sys.stderr, time.strftime("%H:%M:%S", time.localtime()), "Redirection closed"
        sys.stderr = t3.var.original_stderr
        t3.var.ferr.close()


def start():
    global t3
    t3 = Thermeq3Object()
    if not t3.err_str == "":
        print t3.err_str
        exit()

    t3.prepare()

    if mailer.send_error_log(t3.setup, t3.setup.stderr_log):
        os.remove(t3.setup.stderr_log)


def loop():
    global t3
    redirect_error(True)

    while 1:
        if t3.isrt("slp"):
            if t3.intervals() == 0xFF:
                break
        time.sleep(t3.setup.intervals["slp"][0])

    redirect_error(False)