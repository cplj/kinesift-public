"""Command-line entry point for local Kinesift evaluations."""
import argparse
import json
import sys

from . import __version__
from .api import make_server
from .service import MAX_BYTES, error, execute
from .observe import observation
from .harbor import import_harbor

EXIT_CODES = {'SUCCEEDED': 0, 'FAILED': 1, 'ERROR': 2, 'UNKNOWN': 3}


def port_number(value):
    number = int(value)
    if not 0 <= number <= 65535:
        raise argparse.ArgumentTypeError('port must be from 0 through 65535')
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description='Observe local agent containers and evaluate resettable agent tasks.')
    parser.add_argument('--version', action='version', version=__version__)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', help='Reset a task, replay agent actions and verify the final state.')
    run.add_argument('--input', default='-', help='UTF-8 JSON file, or - for stdin (default).')
    serve = commands.add_parser('serve', help='Serve the same command at POST /run on 127.0.0.1.')
    serve.add_argument('--port', type=port_number, default=8765)
    observe = commands.add_parser('observe', help='Observe a retained local Docker container from the host.')
    observe.add_argument('--output', required=True, help='New host evidence directory; never overwritten.')
    observe.add_argument('--timeout', type=float, default=60, help='Maximum elapsed seconds (default 60, limit 3600).')
    observe.add_argument('--sensor-image', default='python:3.14-slim', help='Locally available image with python and Linux libc.')
    observe.add_argument('--watch', action='append', default=None, help='Existing container directory for inotify; repeatable, default /tmp.')
    observe.add_argument('--forbid-path', action='append', default=[], help='Absolute container path whose observed mutation fails; repeatable.')
    observe.add_argument('container_command', nargs=argparse.REMAINDER)
    importing = commands.add_parser('import', help='Append observations to existing local evaluation results.')
    formats = importing.add_subparsers(dest='format', required=True)
    harbor = formats.add_parser('harbor', help='Read a Harbor trial or job directory.')
    harbor.add_argument('directory')
    harbor.add_argument('--observation', help='report.json to attach; input must contain exactly one trial.')
    harbor.add_argument('--output', help='New JSON output file; default <directory>/kinesift-import.json.')
    args = parser.parse_args(argv)
    if args.command == 'observe':
        args.watch = args.watch or ['/tmp']
        payload = observation(args)
        print(json.dumps(payload, allow_nan=False))
        return EXIT_CODES[payload['status']]
    if args.command == 'import':
        payload = import_harbor(args)
        print(json.dumps(payload, allow_nan=False))
        return EXIT_CODES[payload['status']]
    if args.command == 'serve':
        try:
            with make_server(args.port) as server:
                print(json.dumps({'url': f'http://127.0.0.1:{server.server_port}/run',
                                  'version': __version__}), flush=True)
                server.serve_forever()
        except KeyboardInterrupt:
            return 0
        except OSError:
            print(json.dumps(error('SERVER_ERROR', 'Cannot bind the loopback server; choose an available port.')))
            return 2
    else:
        try:
            if args.input == '-':
                raw = sys.stdin.buffer.read(MAX_BYTES + 1)
            else:
                with open(args.input, 'rb') as source:
                    raw = source.read(MAX_BYTES + 1)
            payload = execute(raw)
        except OSError as exc:
            payload = error('INPUT_IO', f'Cannot read input {args.input!r}: {exc.strerror or str(exc)}.',
                            next_action=f'Check that {args.input!r} exists relative to the current directory '
                            'and is a readable file, or use --input - to read stdin. Request schema: '
                            'https://github.com/cplj/kinesift-public/blob/main/docs/schemas/request.json')
        print(json.dumps(payload, allow_nan=False))
        return EXIT_CODES[payload['status']]


if __name__ == '__main__':
    raise SystemExit(main())
