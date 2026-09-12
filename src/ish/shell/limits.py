"""Byte budgets for transport, retained diagnostics, and queued user input.

Equal numeric values do not imply equal policies: changing a diagnostic tail
must not change the transport's event-loop work budget or queue watermarks.
"""

from typing import Final

BYTES_PER_MIB: Final = 1024 * 1024

STREAM_CHUNK_BYTES: Final = 64 * 1024
DIAGNOSTIC_TAIL_BYTES: Final = 64 * 1024
DEFAULT_READ_CHUNK_BYTES: Final = BYTES_PER_MIB
FIFO_READ_CHUNK_BYTES: Final = 4096
TYPEAHEAD_LIMIT_BYTES: Final = BYTES_PER_MIB
OUTPUT_QUEUE_LOW_BYTES: Final = BYTES_PER_MIB
OUTPUT_QUEUE_HIGH_BYTES: Final = 2 * BYTES_PER_MIB
OUTPUT_QUEUE_LIMIT_BYTES: Final = 4 * BYTES_PER_MIB
SCROLLBACK_MAX_LINES: Final = 10000
SCROLLBACK_MAX_BYTES: Final = 10 * BYTES_PER_MIB
