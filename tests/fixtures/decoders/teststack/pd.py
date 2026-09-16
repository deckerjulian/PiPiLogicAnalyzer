"""Stacked decoder used by the test suite (consumes the output of testdec)."""

import sigrokdecode as srd


class Decoder(srd.Decoder):
    api_version = 3
    id = "teststack"
    name = "TestStack"
    longname = "Stacked test decoder"
    desc = "Annotates the edges reported by testdec."
    license = "gplv3+"
    inputs = ["testdec"]
    outputs = []
    tags = ["Util"]
    annotations = (("edge", "Edge"),)
    annotation_rows = (("edges", "Edges", (0,)),)

    def start(self):
        self.out_ann = self.register(srd.OUTPUT_ANN)

    def decode(self, startsample, endsample, data):
        self.put(startsample, endsample, self.out_ann, [0, [f"edge {data[1]}"]])
