"""Suite-wide isolation.

audit_memory promotes audited repair cards to ~/.c2hlsc by default. Tests that drive
run_convert to a pass with applied LLM repairs would otherwise write fake cards into the
developer's REAL prompt-facing memory -- poisoning it with synthetic fixtures. Redirect
the store to a throwaway directory for the whole suite; setdefault keeps a deliberate
override working.
"""

import os
import tempfile

os.environ.setdefault("C2HLSC_MEMORY_DIR", tempfile.mkdtemp(prefix="c2hlsc-test-memory-"))
