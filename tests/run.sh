#!/usr/bin/env bash
# Runs every test: tests/run.sh
set -u
cd "$(dirname "$0")/.."
status=0
echo "== python unit tests"
python3 -m unittest tests/test_transform.py tests/test_touchpad.py || status=1
echo "== python integration tests (real kernel devices)"
python3 -m unittest tests/test_integration.py || status=1
echo "== gjs tests"
gjs -m tests/model.test.js || status=1
exit $status
