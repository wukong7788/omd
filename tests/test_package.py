from importlib.metadata import version

import ohmydata


def test_import_and_version_match_metadata() -> None:
    assert ohmydata.__version__ == "0.1.3"
    assert ohmydata.__version__ == version("ohmydata")
