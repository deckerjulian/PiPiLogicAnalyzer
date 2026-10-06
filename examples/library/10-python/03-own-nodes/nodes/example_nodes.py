"""Own nodes of the example project: every function with @node in nodes/ is a node type.

A plain function is computed from the latest values of its inputs; an ``async def`` that takes
``ctx`` runs with the flow (it can sleep, wait for inputs, emit values).
"""

import numpy as np

from openscilab.core import signals
from openscilab.lab import In, Out, Param, node


@node("example.bit_rate", title="Bit rate",
      inputs=[In("line", "Digital", optional=False)], outputs=[Out("baud", "Scalar")])
def bit_rate(line):
    """The bit rate of a serial line, from its shortest pulse."""
    values = line.values.astype(np.int8)
    edges = np.flatnonzero(np.diff(values) != 0)
    if len(edges) < 2:
        return None
    shortest = np.min(np.diff(edges)) / line.rate
    return signals.Scalar(name="baud", unit="Bd", value=round(1.0 / shortest, -2))


@node("example.blink", title="Blink", outputs=[Out("level", "Bool")],
      params=[Param("period", "quantity", "1 s", "s"), Param("count", "int", 3)])
async def blink(ctx):
    """On and off, 'count' times."""
    period = ctx.quantity("period")
    for _ in range(int(ctx.param("count"))):
        ctx.emit("level", True)
        await ctx.sleep(period / 2)
        ctx.emit("level", False)
        await ctx.sleep(period / 2)
