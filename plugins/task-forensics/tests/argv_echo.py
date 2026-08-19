#!/usr/bin/env python3
"""Test helper: print argv (minus argv[0]) as JSON."""
import json
import sys

print(json.dumps(sys.argv[1:]))
