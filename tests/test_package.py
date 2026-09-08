from hoh import __version__
from hoh.cli import main


def test_package_exports_version() -> None:
    assert __version__ == "0.1.0"


def test_cli_without_command_prints_help(capsys) -> None:
    assert main([]) == 2
    assert "usage: hoh" in capsys.readouterr().err
