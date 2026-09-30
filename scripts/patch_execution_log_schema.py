#!/usr/bin/env python3
"""Pinned Prisma compatibility: unknown execution cost is NULL, never zero."""
from pathlib import Path
import re

def patch(path):
    text = path.read_text()
    pattern = r'(model LiteLLM_SpendLogs \{.*?\n\s+spend\s+)Float(\s+@default\(0\.0\))'
    updated, count = re.subn(pattern, r'\1Float?\2', text, flags=re.S)
    if not count and not re.search(r'model LiteLLM_SpendLogs \{.*?\n\s+spend\s+Float\?', text, re.S):
        raise RuntimeError(f"Revalidate nullable execution cost for changed Prisma schema: {path}")
    path.write_text(updated)

if __name__ == "__main__":
    import sys
    patch(Path(sys.argv[1]))
