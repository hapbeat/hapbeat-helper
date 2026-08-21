"""Allow `python -m hapbeat_helper` to invoke the CLI."""

from hapbeat_helper.cli import main

if __name__ == "__main__":
    # Propagate the exit code — subcommands signal failure with it
    # (`ota` uses 0/1/2), and the console-script entry point does the same.
    raise SystemExit(main())
