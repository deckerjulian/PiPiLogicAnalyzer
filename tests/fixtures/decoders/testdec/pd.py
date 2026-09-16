"""Minimal decoder used by the test suite."""

import sigrokdecode as srd


class Decoder(srd.Decoder):
    api_version = 3
    id = "testdec"
    name = "TestDec"
    longname = "Test decoder"
    desc = "Counts the rising edges of a channel."
    license = "gplv3+"
    inputs = ["logic"]
    outputs = ["testdec"]
    tags = ["Util"]
    channels = ({"id": "clk", "name": "CLK", "desc": "Clock"},)
    optional_channels = ({"id": "aux", "name": "AUX", "desc": "Auxiliary"},)
    options = (
        {"id": "label", "desc": "Label", "default": "edge"},
        {"id": "skip", "desc": "Skip", "default": 0},
        {"id": "mode", "desc": "Mode", "default": "all", "values": ("all", "first")},
    )
    annotations = (("edge", "Edge"), ("info", "Info"))
    annotation_rows = (("edges", "Edges", (0,)), ("infos", "Infos", (1,)))

    def __init__(self):
        super().__init__()
        self.reset()

    def reset(self):
        self.count = 0

    def start(self):
        self.out_ann = self.register(srd.OUTPUT_ANN)
        self.out_python = self.register(srd.OUTPUT_PYTHON)

    def metadata(self, key, value):
        if key == srd.SRD_CONF_SAMPLERATE:
            self.samplerate = value

    def decode(self):
        while True:
            self.wait({0: "r"})
            start = self.samplenum
            self.count += 1
            self.put(start, start + 1, self.out_ann, [0, [f"{self.options['label']} {self.count}"]])
            self.put(start, start + 1, self.out_python, ["EDGE", self.count])
            if self.options["mode"] == "first":
                self.put(start, start + 1, self.out_ann, [1, ["first only"]])
                return
