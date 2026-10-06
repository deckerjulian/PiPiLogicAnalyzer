"""The counter flow of counter.flow.yaml, written with the Python DSL.

    python examples/flows/counter.py                     # runs it (real time)
    openscilab run examples/flows/counter.py --sim --fast
"""

from openscilab.lab import flow, nodes as n

with flow("Counter", description="Captures the counter of the simulator and saves it.") as f:
    sim = f.device("sim", "sim:free")
    cap = n.device.capture(sim, id="cap", channels=["D0", "D1", "D2", "D3", "D8"], rate="4 MHz",
                           samples=20000, trigger=n.edge("D8"))
    cap.capture >> n.view.scope(id="scope", title="Counter")
    cap.capture >> n.data.file(id="file", path="counter.lac")

if __name__ == "__main__":
    print(f.run())
