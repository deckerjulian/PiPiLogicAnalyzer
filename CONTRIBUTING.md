# Contributing

Thanks for your interest in PiPiLogicAnalyzer 7! Bug reports, test results from real hardware, ideas
and pull requests are all welcome. Please be respectful of the people you work with; the
[Code of Conduct](CODE_OF_CONDUCT.md) applies to everything around this project.

Security problems do **not** belong in an issue: see [SECURITY.md](SECURITY.md).

## Reporting a bug

Please open an issue with:

* what you did, what you expected and what happened instead;
* your operating system and how you run the application (downloaded build or from source, with
  the Python version);
* the board and firmware (*Device → Device information... → Copy to clipboard*);
* if possible, a capture file (`.lac`) or a screenshot that shows the problem.

Problems with the hardware design itself are best discussed in the
[original project](https://github.com/gusmanb/logicanalyzer).

## Development setup

```bash
# Linux: Qt needs these system libraries (the CI installs the same ones)
sudo apt-get install -y libegl1 libgl1 libxkbcommon0 libfontconfig1 libdbus-1-3

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
QT_QPA_PLATFORM=offscreen pytest   # the whole suite runs in well under a minute
ruff check .                       # style, configured in pyproject.toml
```

The firmware is built with `firmware/build_all.sh` (see [firmware/README.md](firmware/README.md)).

## Pull requests

* Keep a pull request focused on one change and describe why it is needed.
* Add or update tests for the behaviour you change; the test suite has to pass.
* Follow the style of the surrounding code (`ruff check .`) and keep the user interface texts in
  English.
* Mention user-visible changes in [CHANGELOG.md](CHANGELOG.md).
* Firmware changes: say on which boards you tested them, with real signals if possible.

By contributing you agree that your contribution is licensed under the GNU General Public License
v3, like the rest of the project.
