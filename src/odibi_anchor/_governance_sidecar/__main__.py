"""Binary stdio entry point for the governance sidecar."""

from __future__ import annotations

import sys

from ._crypto import create_process_identity
from ._framing import FramingError, read_frame, write_frame
from ._kernel import Kernel, correlated_error
from ._protocol import EnvelopeError


def main() -> int:
    kernel: Kernel | None = None
    try:
        identity = create_process_identity()
    except Exception:
        return 1
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    try:
        write_frame(stdout, identity.handshake)
        kernel = Kernel()
        while not kernel.shutdown:
            value = read_frame(stdin)
            if value is None:
                kernel.local_shutdown()
                return 0
            try:
                output = kernel.handle(value)
            except EnvelopeError as exc:
                output = correlated_error(exc)
                if output is None:
                    return 1
            write_frame(stdout, output)
        return 0
    except (FramingError, BrokenPipeError, OSError):
        if kernel is not None:
            kernel.local_shutdown()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
