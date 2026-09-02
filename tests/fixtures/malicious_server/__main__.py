"""Run the fixture server standalone.

    python -m tests.fixtures.malicious_server [seconds]

The suite starts this itself, but driving the CLI against it by hand is how
several real bugs were found - the recipe round-trip failure and the
layout-class over-filtering both came from running the actual workflow rather
than from a test. Having a one-line way to do that is worth the file.
"""

from __future__ import annotations

import sys
import time

from tests.fixtures.malicious_server.server import MaliciousServer


def main() -> None:
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    server = MaliciousServer()
    running = server.start()
    print(running.base_url, flush=True)
    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
