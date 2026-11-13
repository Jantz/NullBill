"""The game versions catalog.yaml pins now, for tests of text that names
them: tools/pins moves the pins, and such a test must follow by itself."""

from importlib import resources

import yaml

_games = yaml.safe_load(resources.files("nullbill").joinpath("data/catalog.yaml").read_text())["games"]
MINECRAFT = _games["minecraft"]["version"]
TERRARIA = _games["terraria"]["version"]
FACTORIO = _games["factorio"]["version"]
