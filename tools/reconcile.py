"""Re-query completed provider ranges using the shared ingestion workflow."""

import sys

from bagel_market_data.cli import app

if __name__ == "__main__":
    app(["reconcile", *sys.argv[1:]])
