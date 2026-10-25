"""Convenience entry point; all behavior lives in the component."""

import sys

from bagel_market_data.cli import app

if __name__ == "__main__":
    app(["backfill", *sys.argv[1:]])
