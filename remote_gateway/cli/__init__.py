"""Remote Gateway CLI.

    python -m remote_gateway doctor    # check driver availability & config safety
    python -m remote_gateway start     # start the server
    remote-gateway doctor              # same, once pip-installed (console script)
"""
import argparse
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="remote_gateway", description="Remote Gateway CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="Check driver availability and configuration safety")
    subparsers.add_parser("start", help="Start the gateway server")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if args.command == "doctor":
        from .doctor import main as doctor_main
        doctor_main()
    elif args.command == "start":
        from .start import main as start_main
        start_main()
