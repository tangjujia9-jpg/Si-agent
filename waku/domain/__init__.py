"""Domain contracts shared by the runtime, memory, tools, and providers.

The domain package is deliberately dependency-free.  It is the narrow waist
for the Si-agent architecture: infrastructure adapters can change without
changing the objects that cross subsystem boundaries.
"""

from waku.domain.contracts import (
    Budget,
    MemoryHit,
    MemoryNode,
    OperationStatus,
    RunContext,
    ToolResult,
)

__all__ = [
    "Budget",
    "MemoryHit",
    "MemoryNode",
    "OperationStatus",
    "RunContext",
    "ToolResult",
]
