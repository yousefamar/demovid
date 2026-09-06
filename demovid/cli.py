import argparse
import importlib
import sys

# Each subcommand lives in demovid/<module>/__init__.py exposing
#   add_args(parser: argparse.ArgumentParser) -> None
#   main(ns: argparse.Namespace) -> int
# Keep heavy imports (cv2, av, pywayland) inside main(), not at module top.
SUBCOMMANDS = {
    "rec": ("demovid.rec", "Toggle a recording (start if idle, stop if live)"),
    "render": ("demovid.render", "Render a recording dir into a demo mp4"),
    "upload": ("demovid.upload", "Upload an mp4 to YouTube (unlisted) and copy the URL"),
    "import-screenix": ("demovid.screenix", "Convert a Screenix recording dir into demovid's layout"),
    "menu": ("demovid.menu", "The waybar button's right-click menu (start/stop, capture toggles, follow-ups)"),
    "doctor": ("demovid.doctor", "Check machine prerequisites; --restore undoes a crashed rec"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="demovid")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, (module_path, help_text) in SUBCOMMANDS.items():
        module = importlib.import_module(module_path)
        p = sub.add_parser(name, help=help_text)
        module.add_args(p)
        p.set_defaults(func=module.main)
    return parser


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    return ns.func(ns)


if __name__ == "__main__":
    sys.exit(main())
