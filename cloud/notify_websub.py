#!/usr/bin/env python3
"""Notify the public Google WebSub hub after a successful Pages deployment."""

from __future__ import annotations

import argparse
import sys
import urllib.parse
import urllib.request
from typing import Sequence


DEFAULT_HUB = "https://pubsubhubbub.appspot.com/"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("feeds", nargs="+")
    parser.add_argument("--hub", default=DEFAULT_HUB)
    args = parser.parse_args(argv)
    fields = [("hub.mode", "publish"), *(("hub.url", feed) for feed in args.feeds)]
    body = urllib.parse.urlencode(fields).encode("ascii")
    request = urllib.request.Request(
        args.hub,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "SSD-Research-Radar/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status >= 300:
                raise RuntimeError(f"hub returned HTTP {response.status}")
        print(f"Notified WebSub hub for {len(args.feeds)} feed(s)")
        return 0
    except Exception as error:
        print(f"WebSub notification failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
