# Contributing

Thanks for your interest in openSciLab! Bug reports, test results from real hardware, ideas
and pull requests are all welcome. Please be respectful of the people you work with; the
[Code of Conduct](CODE_OF_CONDUCT.md) applies to everything around this project.

Security problems do **not** belong in an issue: see [SECURITY.md](SECURITY.md).

## Reporting a bug

Please open an issue with:

* what you did, what you expected and what happened instead;
* your operating system and how you run the application (downloaded build or from source, with
  the Python version);
* the device and its firmware (the *Details* tab of its device card → *Copy the details*);
* if possible, a capture file (`.lac`), the project or flow (`*.flow.yaml`) or a screenshot that
  shows the problem - many problems can be shown with a simulator (`sim:uno`, `sim:pico`, ...).

openSciLab is a beta: reports of what works on real hardware, and what does not, are especially
welcome. Problems with the Pico logic analyzer hardware design itself are best discussed in the
[original project](https://github.com/gusmanb/logicanalyzer).

## Development setup

```bash
# Linux: Qt needs these system libraries (the CI installs the same ones)
sudo apt-get install -y libegl1 libgl1 libxkbcommon0 libfontconfig1 libdbus-1-3

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
QT_QPA_PLATFORM=offscreen pytest   # the whole suite (about 2000 tests) takes a few minutes
ruff check .                       # style, configured in pyproject.toml
```

The firmware is built with `firmware/build_all.sh` (see [firmware/README.md](firmware/README.md)).
New devices are added as drivers or plugins, see [docs/drivers.md](docs/drivers.md); the user
documentation is in the [wiki](https://github.com/deckerjulian/openSciLab/wiki).

## Pull requests

* Keep a pull request focused on one change and describe why it is needed.
* Add or update tests for the behaviour you change; the test suite has to pass. New behaviour
  works with a simulator first, so it can be tested without hardware.
* Follow the style of the surrounding code (`ruff check .`) and keep the user interface texts in
  English.
* Mention user-visible changes in [CHANGELOG.md](CHANGELOG.md).
* Firmware changes: say on which boards you tested them, with real signals if possible.

By contributing you agree that your contribution is licensed under the GNU General Public License
v3, like the rest of the project.
