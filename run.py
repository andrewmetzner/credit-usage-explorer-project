import argparse

from app import create_app


def parse_args():
    parser = argparse.ArgumentParser(description="Run the Credit Usage Explorer web app.")
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="Interface to bind to. Use 0.0.0.0 to make the app reachable from other "
             "machines on the network (default: 127.0.0.1, local machine only).",
    )
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on (default: 5000).")
    parser.add_argument(
        "--server", choices=("flask", "waitress"), default="flask",
        help="'flask' (default) is Flask's built-in dev server :: auto-reload, debugger, "
             "single-threaded, fine for local use. 'waitress' is a production-grade WSGI "
             "server (multi-threaded, no debugger/reloader) for sharing the app on a "
             "network; requires the 'waitress' package (pip install waitress).",
    )
    parser.add_argument(
        "--debug", dest="debug", action="store_true", default=True,
        help="Enable Flask's debug mode/reloader (default: on). Ignored with --server waitress.",
    )
    parser.add_argument(
        "--no-debug", dest="debug", action="store_false",
        help="Disable debug mode/reloader — recommended whenever --host is not 127.0.0.1.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    app = create_app()

    if args.server == "waitress":
        try:
            from waitress import serve
        except ImportError:
            raise SystemExit(
                "The 'waitress' package isn't installed. Run: pip install waitress"
            )
        print(f"Serving on http://{args.host}:{args.port} (waitress)")
        serve(app, host=args.host, port=args.port)
    else:
        app.run(host=args.host, port=args.port, debug=args.debug)
