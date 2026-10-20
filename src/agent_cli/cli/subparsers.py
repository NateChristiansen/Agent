"""The type every `add_parser(sub)` takes: the action `ArgumentParser.add_subparsers` returns."""

import argparse

# argparse has no public name for this class; typeshed only exposes the private one.
type Subparsers = argparse._SubParsersAction[argparse.ArgumentParser]  # pyright: ignore[reportPrivateUsage]
