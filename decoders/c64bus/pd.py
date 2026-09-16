##
## This file is part of the PiPiLogicAnalyzer project.
##
## C64 / 6502 system bus decoder for sigrok (libsigrokdecode API).
##
## Copyright (C) 2026 Julian Decker
##
## SPDX-License-Identifier: GPL-2.0-or-later
##
## This program is free software: you can redistribute it and/or modify
## it under the terms of the GNU General Public License as published by
## the Free Software Foundation, either version 2 of the License, or
## (at your option) any later version.
##

import sigrokdecode as srd

from .disasm import Cycle, Disassembler

# Memory map of the C64 (standard configuration after a reset).
# (first address, last address, short name, long name)
C64_MAP = (
    (0x0000, 0x0001, 'CPU port',  '6510 processor port (D6510/R6510)'),
    (0x0002, 0x00FF, 'Zero page', 'Zero page'),
    (0x0100, 0x01FF, 'Stack',     'Processor stack'),
    (0x0200, 0x03FF, 'System',    'System variables / vectors'),
    (0x0400, 0x07FF, 'Screen',    'Screen memory'),
    (0x0800, 0x9FFF, 'RAM',       'BASIC RAM'),
    (0xA000, 0xBFFF, 'BASIC',     'BASIC ROM'),
    (0xC000, 0xCFFF, 'RAM',       'Free RAM'),
    (0xD000, 0xD3FF, 'VIC',       'VIC-II'),
    (0xD400, 0xD7FF, 'SID',       'SID'),
    (0xD800, 0xDBFF, 'Color RAM', 'Color RAM'),
    (0xDC00, 0xDCFF, 'CIA1',      'CIA 1 (keyboard/joystick)'),
    (0xDD00, 0xDDFF, 'CIA2',      'CIA 2 (IEC/user port)'),
    (0xDE00, 0xDEFF, 'IO1',       'I/O area 1 (expansion port)'),
    (0xDF00, 0xDFFF, 'IO2',       'I/O area 2 (expansion port)'),
    (0xE000, 0xFFF9, 'KERNAL',    'KERNAL ROM'),
    (0xFFFA, 0xFFFB, 'NMI vec',   'NMI vector'),
    (0xFFFC, 0xFFFD, 'RES vec',   'Reset vector'),
    (0xFFFE, 0xFFFF, 'IRQ vec',   'IRQ/BRK vector'),
)

# Pin names of the expansion port. The channel names match the names in the
# profile examples/c64-expansion-port-profile.json exactly, so the channels are
# assigned automatically when the decoder is added.
ADDR_PINS = 'YXWVUTSRPNMLKJHF'   # A0..A15
DATA_PINS = (21, 20, 19, 18, 17, 16, 15, 14)   # D0..D7

# Active low select lines of the cartridge: (id, channel name, short, long)
SELECT_LINES = (
    ('roml', '/ROML (11)', 'ROML', 'Cartridge ROM low (/ROML)'),
    ('romh', '/ROMH (B)',  'ROMH', 'Cartridge ROM high (/ROMH)'),
    ('io1',  '/IO1 (7)',   'IO1',  'I/O area 1 (/IO1)'),
    ('io2',  '/IO2 (10)',  'IO2',  'I/O area 2 (/IO2)'),
)

# Active low control lines: (id, channel name, short)
CONTROL_LINES = (
    ('reset', '/RESET (C)', 'RESET'),
    ('irq',   '/IRQ (4)',   'IRQ'),
    ('nmi',   '/NMI (D)',   'NMI'),
)

READ_BEFORE_EDGE = 'before falling edge'
READ_AT_EDGE = 'at falling edge'

# Pins checked for changes around the read point, with their names.
BUS_NAMES = dict([(i, 'A%d' % i) for i in range(16)] + [(16 + i, 'D%d' % i) for i in range(8)]
                 + [(25, 'R/W')])


def region_of(addr):
    for start, end, short, long in C64_MAP:
        if start <= addr <= end:
            return short, long
    return '?', 'unknown'


def unique(texts):
    result = []
    for text in texts:
        if text not in result:
            result.append(text)
    return result


class Decoder(srd.Decoder):
    api_version = 3
    id = 'c64bus'
    name = 'C64 bus'
    longname = 'Commodore 64 / 6502 system bus'
    desc = 'Decodes 6502/6510 bus cycles, the C64 memory regions and disassembles the code.'
    license = 'gplv2+'
    inputs = ['logic']
    outputs = []
    tags = ['Retro computing', 'Parallel']

    # The order defines the index in the pins tuple of wait():
    # 0..15 = A0..A15, 16..23 = D0..D7, 24 = PHI2, 25 = R/W
    channels = (
        tuple({'id': 'a%d' % i, 'name': 'A%d (%s)' % (i, ADDR_PINS[i]),
               'desc': 'Address bit %d' % i} for i in range(16))
        + tuple({'id': 'd%d' % i, 'name': 'D%d (%d)' % (i, DATA_PINS[i]),
                 'desc': 'Data bit %d' % i} for i in range(8))
        + ({'id': 'phi2', 'name': 'Φ2 (E)', 'desc': 'System clock phase 2'},
           {'id': 'rw', 'name': 'R/W (5)', 'desc': 'Read (high) / write (low)'})
    )

    # 26 = BA, 27..29 = /RESET, /IRQ, /NMI, 30..33 = /ROML, /ROMH, /IO1, /IO2
    optional_channels = (
        ({'id': 'ba', 'name': 'BA (12)', 'desc': 'Bus available (VIC)'},)
        + tuple({'id': cid, 'name': cname, 'desc': 'Control line %s' % short}
                for cid, cname, short in CONTROL_LINES)
        + tuple({'id': cid, 'name': cname, 'desc': long}
                for cid, cname, short, long in SELECT_LINES)
    )

    options = (
        {'id': 'fmt', 'desc': 'Address format', 'default': 'hex',
         'values': ('hex', 'dec')},
        {'id': 'regions', 'desc': 'Show memory regions',
         'default': 'yes', 'values': ('yes', 'no')},
        # The 6510 takes the data at the falling PHI2 edge; the first sample after the edge
        # may already show the next address. Only PiPiLogicAnalyzer can look back one sample,
        # other hosts read at the edge.
        {'id': 'read', 'desc': 'Read the bus',
         'default': READ_BEFORE_EDGE, 'values': (READ_BEFORE_EDGE, READ_AT_EDGE)},
        {'id': 'offset', 'desc': 'Read offset (samples)', 'default': 0},
        {'id': 'disasm', 'desc': 'Disassemble', 'default': 'yes', 'values': ('yes', 'no')},
    )

    annotations = (
        ('read', 'Read cycle'),
        ('write', 'Write cycle'),
        ('stolen', 'VIC cycle'),
        ('region', 'Memory region'),
        ('signal', 'Control line active'),
        ('instruction', 'Instruction'),
        ('interrupt', 'Interrupt'),
        ('unstable', 'Bus unstable'),
    )

    annotation_rows = (
        ('cycles', 'Bus cycles', (0, 1, 2)),
        ('mem', 'Memory region', (3,)),
        ('signals', 'Control lines', (4,)),
        ('asm', 'Disassembly', (5, 6)),
        ('warnings', 'Warnings', (7,)),
    )

    # Pin indexes as constants to keep the code readable.
    PHI2, RW, BA = 24, 25, 26
    CONTROL_BASE = 27
    SELECT_BASE = CONTROL_BASE + len(CONTROL_LINES)

    def __init__(self):
        self.reset()

    def reset(self):
        self.last_region = None
        self.last_region_long = None
        self.region_ss = 0
        # Start of the low phase of every control line (None = inactive).
        self.control_ss = [None] * len(CONTROL_LINES)
        self.disasm = Disassembler()
        self.put_sample_point = None
        self.instructions = 0
        self.first_cycle_ss = None
        self.last_cycle_es = None

    def start(self):
        self.out_ann = self.register(srd.OUTPUT_ANN)

    def fmt_addr(self, addr):
        if self.options['fmt'] == 'dec':
            return '%d' % addr
        return '$%04X' % addr

    def cycle_texts(self, kind, addr, data, note, doubt):
        # Long to short: hosts show the longest text that fits, so the value read or
        # written stays visible down to two characters.
        a = self.fmt_addr(addr)
        compact = ('%d' if self.options['fmt'] == 'dec' else '%04X') % addr
        return unique(['%s %s = $%02X%s' % (kind, a, data, note),
                       '%s %s = $%02X%s' % (kind, a, data, doubt),
                       '%s %s=$%02X%s' % (kind, a, data, doubt),
                       '%s=%02X%s' % (compact, data, doubt),
                       '%02X%s' % (data, doubt),
                       kind])

    def put_region(self, ss, es, short, long):
        # 'nein' is the value stored by profiles of the German first version.
        if self.options['regions'] in ('no', 'nein'):
            return
        self.put(ss, es, self.out_ann, [3, [long, short]])

    def put_events(self, events):
        for event in events:
            # Hovering an instruction shows the bus at its opcode fetch.
            self.put_sample_point = event.sample
            kind = 5 if event.kind == 'instruction' else 6
            self.instructions += 1
            self.put(event.ss, event.es, self.out_ann, [kind, event.texts])
        self.put_sample_point = None

    def is_low(self, pins, idx):
        return self.has_channel(idx) and pins[idx] == 0

    def region_for_cycle(self, pins, addr):
        # Active select lines of the cartridge take precedence over the address,
        # they reflect the actual memory configuration.
        for i, (cid, cname, short, long) in enumerate(SELECT_LINES):
            if self.is_low(pins, self.SELECT_BASE + i):
                return short, long
        return region_of(addr)

    def track_controls(self, pins, ss):
        for i, (cid, cname, short) in enumerate(CONTROL_LINES):
            active = self.is_low(pins, self.CONTROL_BASE + i)
            if active and self.control_ss[i] is None:
                self.control_ss[i] = ss
            elif not active and self.control_ss[i] is not None:
                self.put_control(i, ss)

    def put_control(self, i, es):
        short = CONTROL_LINES[i][2]
        self.put(self.control_ss[i], es, self.out_ann,
                 [4, ['%s active' % short, short]])
        self.control_ss[i] = None

    def flush(self, es):
        if self.last_region is not None:
            self.put_region(self.region_ss, es,
                            self.last_region, self.last_region_long)
            self.last_region = None
        for i in range(len(CONTROL_LINES)):
            if self.control_ss[i] is not None:
                self.put_control(i, es)
        if getattr(self, 'options', {}).get('disasm') != 'no':
            self.put_events(self.disasm.finish())
            if not self.instructions and self.first_cycle_ss is not None:
                # Without a hint the Disassembly row would simply be missing.
                self.put(self.first_cycle_ss, self.last_cycle_es, self.out_ann,
                         [6, ['No disassembly: the program flow could not be followed. The bus '
                              'changes at the read point; align the boards (Capture > Align boards) '
                              'or set a read offset.', 'No disassembly', '?']])

    def read_bus(self, peek, ss, edge, es, edge_pins):
        '''(sample, pins, names of bus lines changing next to the sample).'''
        if peek is None:
            return edge, edge_pins, []
        base = edge - 1 if self.options['read'] != READ_AT_EDGE else edge
        sample = min(max(base + int(self.options['offset']), ss), es - 1)
        before = peek(sample - 1) if sample > 0 else None
        after = peek(sample + 1)
        pins = peek(sample)  # looked at last: the sample point of the cycle
        unstable = [name for pin, name in sorted(BUS_NAMES.items())
                    if (before is not None and before[pin] != pins[pin]) or after[pin] != pins[pin]]
        return sample, pins, unstable

    def decode(self):
        try:
            self.decode_cycles()
        finally:
            # Close open regions and signals at the end of the capture.
            self.flush(self.samplenum)

    def decode_cycles(self):
        peek = getattr(self, 'pins_at', None)
        disassemble = self.options['disasm'] != 'no'

        # Synchronise on the first rising PHI2 edge.
        self.wait({self.PHI2: 'r'})

        while True:
            ss = self.samplenum

            # The 6510 latches the data at the falling PHI2 edge.
            edge_pins = self.wait({self.PHI2: 'f'})
            edge = self.samplenum

            # The cycle ends at the next rising edge.
            self.wait({self.PHI2: 'r'})
            es = self.samplenum

            sample, pins, unstable = self.read_bus(peek, ss, edge, es, edge_pins)
            if self.first_cycle_ss is None:
                self.first_cycle_ss = ss
            self.last_cycle_es = es

            addr = 0
            for i in range(16):
                addr |= pins[i] << i

            data = 0
            for i in range(8):
                data |= pins[16 + i] << i

            rw = pins[self.RW]
            short, long = self.region_for_cycle(pins, addr)
            self.track_controls(pins, ss)

            a = self.fmt_addr(addr)
            vic = self.is_low(pins, self.BA)
            note = ' (unstable: %s)' % ' '.join(unstable) if unstable else ''
            doubt = '?' if unstable else ''

            if vic:
                # BA low: the VIC took the bus from the CPU.
                self.put(ss, es, self.out_ann,
                         [2, ['VIC %s' % a, 'VIC', 'V']])
            elif rw:
                self.put(ss, es, self.out_ann,
                         [0, self.cycle_texts('R', addr, data, note, doubt)])
            else:
                self.put(ss, es, self.out_ann,
                         [1, self.cycle_texts('W', addr, data, note, doubt)])

            if unstable:
                self.put(ss, es, self.out_ann,
                         [7, ['Bus changing at the read point: %s' % ', '.join(unstable),
                              'Unstable', '!']])

            if disassemble and not vic:
                self.put_events(self.disasm.feed(Cycle(ss, es, sample, addr, data, bool(rw))))

            # Merge consecutive accesses to the same region.
            if short != self.last_region:
                if self.last_region is not None:
                    self.put_region(self.region_ss, ss,
                                    self.last_region, self.last_region_long)
                self.last_region = short
                self.last_region_long = long
                self.region_ss = ss
