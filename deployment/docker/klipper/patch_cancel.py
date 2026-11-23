"""Adapt print pacing, cancellation, and motion scheduling for the AVR simulator.

Apply only to the pinned upstream Klipper source during image creation.
Fail the build if that source changes so an upstream update cannot silently
leave the cancellation path unpatched.
"""

from pathlib import Path


def replace_once(path, before, after):
    source = path.read_text()
    if source.count(before) != 1:
        raise RuntimeError(f'Unexpected pinned Klipper source in {path}')
    path.write_text(source.replace(before, after))


root = Path('/opt/klipper/klippy/extras')
replace_once(
    root / 'heaters.py',
    '        pwm_time = read_time + self.pwm_delay\n',
    '''        # The AVR's clock can catch up after a host scheduling delay.
        # Schedule from the live MCU clock if the ADC sample is already old.
        mcu = self.mcu_pwm.get_mcu()
        now = self.printer.get_reactor().monotonic()
        # Leave enough lead time for serial dispatch and Docker jitter.
        pwm_time = max(read_time + self.pwm_delay,
                       mcu.estimated_print_time(now) + .75)
''',
)
replace_once(
    root / 'pause_resume.py',
    '''    def _handle_cancel_request(self, web_request):
        self.gcode.run_script("CANCEL_PRINT")
''',
    '''    def _handle_cancel_request(self, web_request):
        # Wake cooperative virtual SD waits before taking the G-code mutex.
        if self.is_sd_active() or self.sd_paused:
            self.v_sd.cancel_requested = True
            self.v_sd.must_pause_work = True
        self.gcode.run_script("CANCEL_PRINT")
''',
)
replace_once(
    root / 'heaters.py',
    '''        while not self.printer.is_shutdown() and heater.check_busy(eventtime):
            print_time = toolhead.get_last_move_time()
''',
    '''        while not self.printer.is_shutdown() and heater.check_busy(eventtime):
            current, target = heater.get_temp(eventtime)
            if _virtual_sd_cancelling(self.printer):
                break
            if target <= 0. or current >= target - 2.:
                break
            print_time = toolhead.get_last_move_time()
''',
)
replace_once(
    root / 'heaters.py',
    '''        with self.lock:
            self.target_temp = degrees
    def get_temp(self, eventtime):
''',
    '''        with self.lock:
            self.target_temp = degrees
        # Publish the validated target to the owned AVR thermal simulator.
        with open('/data/thermal_targets/' + self.short_name, 'w') as target_file:
            target_file.write(str(degrees))
    def get_temp(self, eventtime):
''',
)

replace_once(
    root / 'pause_resume.py',
    '''        if self.is_sd_active() or self.sd_paused:
            self.v_sd.do_cancel()
''',
    '''        if self.v_sd is not None and (self.is_sd_active() or self.sd_paused
                                      or self.v_sd.cancel_requested):
            self.v_sd.do_cancel()
            self.printer.lookup_object('heaters').turn_off_all_heaters()
''',
)

replace_once(
    root / 'pause_resume.py',
    '''        self.cmd_CLEAR_PAUSE(gcmd)
''',
    '''        self.sd_paused = False
        self.cmd_CLEAR_PAUSE(gcmd)
''',
)

helper = '''def _virtual_sd_cancelling(printer):
    sdcard = printer.lookup_object('virtual_sdcard', None)
    return sdcard is not None and sdcard.cancel_requested


'''
heaters = root / 'heaters.py'
replace_once(heaters, 'def load_config(config):', helper + 'def load_config(config):')
replace_once(
    heaters,
    '''        while not self.printer.is_shutdown():
            temp, target = sensor.get_temp(eventtime)
''',
    '''        while not self.printer.is_shutdown():
            if _virtual_sd_cancelling(self.printer):
                return
            temp, target = sensor.get_temp(eventtime)
''',
)
heater_source = heaters.read_text()
if heater_source.count('reactor.pause(eventtime + 1.)') != 2:
    raise RuntimeError('Unexpected pinned heater wait intervals')
heaters.write_text(heater_source.replace('reactor.pause(eventtime + 1.)',
                                         'reactor.pause(eventtime + .1)'))

sdcard = root / 'virtual_sdcard.py'
replace_once(
    sdcard,
    '''        self.next_file_position = 0
''',
    '''        self.next_file_position = 0
        self.cancel_requested = False
        self.progress_percent = 0
        self.next_progress_at = 0.
''',
)
replace_once(
    sdcard,
    '''        if self.file_size:
            return float(self.file_position) / self.file_size
        else:
            return 0.
''',
    '''        if self.progress_percent in (0, 100):
            return self.progress_percent / 100.
        # Mainsail floors percentage values. Prevent binary rounding from
        # showing a repeated step at 29%, 57%, or 58%.
        return (self.progress_percent + 1.e-9) / 100.
    def _pace_progress(self, target):
        # Advance only through bytes that have already been processed. Keep
        # a full display interval between updates even after a long wait.
        while self.progress_percent < target and not self.must_pause_work:
            now = self.reactor.monotonic()
            if now < self.next_progress_at:
                self.reactor.pause(min(self.next_progress_at, now + .05))
                continue
            self.progress_percent += 1
            self.next_progress_at = now + .8
''',
)
replace_once(
    sdcard,
    '''        self.must_pause_work = False
        self.work_timer = self.reactor.register_timer(
''',
    '''        self.must_pause_work = False
        self.next_progress_at = self.reactor.monotonic() + .8
        self.work_timer = self.reactor.register_timer(
''',
)
replace_once(
    sdcard,
    '''            self.print_stats.note_cancel()
        self.file_position = self.file_size = 0
''',
    '''            self.print_stats.note_cancel()
        self.file_position = self.file_size = self.progress_percent = 0
        self.cancel_requested = False
''',
)
replace_once(
    sdcard,
    '''        self.file_size = fsize
        self.print_stats.set_current_file(filename)
''',
    '''        self.file_size = fsize
        self.progress_percent = 0
        self.print_stats.set_current_file(filename)
''',
)
replace_once(
    sdcard,
    '''        self.print_stats.reset()
        self.printer.send_event("virtual_sdcard:reset_file")
''',
    '''        self.progress_percent = 0
        self.print_stats.reset()
        self.printer.send_event("virtual_sdcard:reset_file")
''',
)
replace_once(
    sdcard,
    '''                if not data:
                    # End of file
                    self.current_file.close()
''',
    '''                if not data:
                    if partial_input:
                        # A final command need not end with a newline.
                        lines = [partial_input]
                        partial_input = ""
                        continue
                    self._pace_progress(100)
                    if self.must_pause_work:
                        break
                    self.current_file.close()
''',
)
replace_once(
    sdcard,
    '''            self.file_position = self.next_file_position
            # Do we need to skip around?
''',
    '''            self.file_position = min(self.next_file_position, self.file_size)
            if self.file_size:
                self._pace_progress(min(99, self.file_position * 100
                                             // self.file_size))
            # Do we need to skip around?
''',
)
replace_once(
    root / 'display_status.py',
    '''        progress = self.progress
''',
    '''        sdcard = self.printer.lookup_object('virtual_sdcard', None)
        if sdcard is not None and (sdcard.current_file is not None
                                   or sdcard.progress_percent == 100):
            return {'progress': sdcard.progress(), 'message': self.message}
        progress = self.progress
''',
)

toolhead = root.parent / 'toolhead.py'
replace_once(
    sdcard,
    '''            try:
                self.gcode.run_script(line)
            except self.gcode.error as e:
''',
    '''            # Trace actual virtual SD dispatch, including commands that
            # block while the simulated printer heats or waits for motion.
            command = line.partition(';')[0].strip()
            command = ''.join(c for c in command if c.isprintable())[:180]
            opcode = command.split(None, 1)[0].upper() if command else ''
            waiting = {
                'M109': 'waiting for hotend',
                'M190': 'waiting for bed',
                'TEMPERATURE_WAIT': 'waiting for temperature',
                'M400': 'waiting for queued moves',
                'G4': 'dwelling',
            }.get(opcode)
            if command:
                self.gcode.respond_info(
                    "G-code: %s [%s]" % (command, waiting or 'executing'),
                    log=False)
            try:
                self.gcode.run_script(line)
                if waiting and not self.cancel_requested and not self.printer.is_shutdown():
                    self.gcode.respond_info(
                        "G-code: %s [wait finished]" % command, log=False)
            except self.gcode.error as e:
''',
)
replace_once(
    sdcard,
    '''    def stats(self, eventtime):
        if self.work_timer is None:
''',
    '''    def stats(self, eventtime):
        # respond_info is forwarded by Moonraker to the Mainsail console.
        job = self.print_stats.get_status(eventtime)
        state, filename = job['state'], job['filename']
        percent = self.progress_percent
        update = (filename, state, percent // 10 if state == 'printing' else 0)
        if update != getattr(self, '_last_console_job', None):
            self._last_console_job = update
            if filename and state in ('printing', 'paused', 'complete',
                                      'cancelled', 'error'):
                safe_name = ''.join(c for c in filename if c.isprintable())[:160]
                self.gcode.respond_info(
                    "Print %s: %s (%d%%)" % (state, safe_name, percent),
                    log=False)
        if self.work_timer is None:
''',
)
# Give the emulated AVR a wider dispatch window when Docker is busy.
replace_once(toolhead, 'BUFFER_TIME_START = 0.250', 'BUFFER_TIME_START = 0.500')
replace_once(
    root.parent / 'chelper' / 'serialqueue.c',
    '#define MIN_REQTIME_DELTA 0.100',
    '#define MIN_REQTIME_DELTA 0.500',
)
replace_once(toolhead, 'class ToolHead:', helper + 'class ToolHead:')
replace_once(
    toolhead,
    '''            pause_time = max(.005, min(1., pause_time))
''',
    '''            if _virtual_sd_cancelling(self.printer):
                break
            pause_time = max(.005, min(.1, pause_time))
''',
)
replace_once(
    toolhead,
    '''            if not self.can_pause:
                break
            eventtime = self.reactor.pause(eventtime + 0.100)
''',
    '''            if not self.can_pause or _virtual_sd_cancelling(self.printer):
                break
            eventtime = self.reactor.pause(eventtime + 0.100)
''',
)
replace_once(
    toolhead,
    '''        self.toolhead.dwell(delay)
    def cmd_M400(self, gcmd):
''',
    '''        sdcard = self.printer.lookup_object('virtual_sdcard', None)
        if sdcard is None or not sdcard.cmd_from_sd:
            self.toolhead.dwell(delay)
            return
        self.toolhead.wait_moves()
        reactor = self.printer.get_reactor()
        deadline = reactor.monotonic() + delay
        now = reactor.monotonic()
        while now < deadline and not sdcard.cancel_requested:
            now = reactor.pause(min(deadline, now + .1))
    def cmd_M400(self, gcmd):
''',
)
