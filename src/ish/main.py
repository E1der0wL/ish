"""Module and console entry points that parse CLI options and start ish sessions."""

from __future__ import annotations

from ish.parser.cli import ArgumentParser


def main():
    """Parse command-line arguments and run the selected interactive shell UI."""
    option = ArgumentParser()
    option.parse()


if __name__ == "__main__":
    main()
