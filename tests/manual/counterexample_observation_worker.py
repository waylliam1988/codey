"""Fixed synchronous function invocation; model controls JSON arguments only."""
import contextlib
import io
import json
import sys


def main():
    spec = json.load(sys.stdin)
    rows = []
    for probe in spec['probes']:
        # Fresh module and input per probe; no cross-probe mutation or expected value.
        namespace = {'__name__': 'frozen_candidate'}
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                exec(compile(spec['source'], 'app.py', 'exec'), namespace)
                value = namespace['select'](*probe['args'], **probe['kwargs'])
            json.dumps(value, allow_nan=False)
            rows.append({'status': 'returned', 'value': value})
        except Exception as exc:
            rows.append({'status': 'raised', 'exception_type': type(exc).__name__, 'message': str(exc)[:1000]})
    print(json.dumps({'observations': rows}, allow_nan=False))


if __name__ == '__main__':
    main()
