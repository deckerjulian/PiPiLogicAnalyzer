##
## This file is part of the PiPiLogicAnalyzer project.
##
## C64 / 6502 system bus decoder for sigrok.
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

'''
Decodes the system bus of a 6502/6510 computer (address, data, R/W)
and labels the memory regions of the Commodore 64.
'''

from .pd import Decoder
