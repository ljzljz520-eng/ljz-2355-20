#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 tests/acceptance.py
python3 tests/smoke_http.py
