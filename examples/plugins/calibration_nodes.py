# Copyright (C) 2026 Julian Decker
#
# Part of openSciLab.
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""An example plugin: nodes of its own for the flows (``docs/drivers.md``, *Plugins*).

Copy this file into the ``plugins`` folder of the settings directory (``openscilab-cli plugins``
names it) or point ``OPENSCILAB_PLUGINS`` at this folder, then start openSciLab: the node palette
shows the group *Calibration* with *Two-point calibration* and *NTC thermistor*, and every flow
and project can use them - ``{type: calib.ntc, ...}`` in a flow file, ``openscilab run`` too.

A node is a function with ``@node`` (as the own nodes of a project in its ``nodes/*.py``, see
``docs/lab.md``): a plain function is computed from the latest values of its inputs whenever one
changes, an ``async def`` taking ``ctx`` runs with the flow.
"""

from __future__ import annotations

import math

from openscilab.core import signals
from openscilab.lab import In, Out, Param, node, node_group

node_group("calib", "Calibration")


@node("calib.linear", title="Two-point calibration", icon="ruler",
      inputs=[In("raw", "Scalar", optional=False)], outputs=[Out("value", "Scalar")],
      params=[Param("raw_low", "float", 0.0), Param("raw_high", "float", 1.0),
              Param("low", "float", 0.0, description="what 'raw_low' stands for"),
              Param("high", "float", 1.0, description="what 'raw_high' stands for"),
              Param("unit", "str", "")])
def linear(raw, params):
    """A raw reading as what it measures, by two points of reference: 'raw_low' is 'low',
    'raw_high' is 'high' (a sensor's volts as a pressure, counts as volts)."""
    span = float(params["raw_high"]) - float(params["raw_low"])
    if span == 0:
        raise ValueError("raw_low and raw_high must differ")
    share = (float(raw) - float(params["raw_low"])) / span
    value = float(params["low"]) + share * (float(params["high"]) - float(params["low"]))
    return signals.Scalar(name="value", unit=str(params.get("unit") or ""), value=value, at=getattr(raw, "at", 0.0))


@node("calib.ntc", title="NTC thermistor", icon="target",
      inputs=[In("volts", "Scalar", optional=False)], outputs=[Out("temperature", "Scalar")],
      params=[Param("supply", "float", 3.3, description="the voltage across the divider (V)"),
              Param("fixed", "float", 10_000.0, description="the fixed resistor to the supply (Ω)"),
              Param("r25", "float", 10_000.0, description="the thermistor at 25 °C (Ω)"),
              Param("beta", "float", 3950.0, description="its B constant (K)")])
def ntc(volts, params):
    """The temperature of an NTC thermistor between an analog input and ground, with a fixed
    resistor to the supply (B equation)."""
    supply, measured = float(params["supply"]), float(volts)
    if not 0 < measured < supply:
        return None  # (open or shorted: no temperature)
    resistance = float(params["fixed"]) * measured / (supply - measured)
    kelvin = 1.0 / (1.0 / 298.15 + math.log(resistance / float(params["r25"])) / float(params["beta"]))
    return signals.Scalar(name="temperature", unit="°C", value=round(kelvin - 273.15, 3), at=getattr(volts, "at", 0.0))
