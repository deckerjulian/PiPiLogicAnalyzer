"""Captures the counter of the simulator, measures its clock and saves the capture - as a script.

    python flows/counter.py                     # runs it (real time)
    openscilab run flows/counter.py --fast      # the same from the command line, in virtual time
"""

from openscilab.lab import flow
from openscilab.lab import nodes as n

with flow("Counter", description="Captures the counter of the simulator and saves it.") as f:
    la = f.device("la", "sim:free")
    capture = n.device.capture(la, id="capture", channels=["D0", "D1", "D2", "D3", "D8"], rate="4 MHz",
                               samples=20000, trigger=n.edge("D8"))
    capture.capture >> n.view.scope(id="scope", title="Counter")
    capture.capture >> n.data.file(id="file", path="counter.lac")
    capture.D8 >> n.measure.frequency(id="clock") >> n.report.check(id="check", name="Clock", expected=1e6,
                                                                    tolerance=1e3, unit="Hz")

if __name__ == "__main__":
    print(f.run())
