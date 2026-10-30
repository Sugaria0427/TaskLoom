"""First Fit over an actual application-owned buffer, not the OS allocator."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Block:
    identity: int
    offset: int
    size: int


class FirstFit:
    def __init__(self, capacity: int) -> None:
        if not 1 <= capacity <= 16 * 1048576:
            raise ValueError("Controlled buffer capacity must be 1 byte to 16 MiB")
        self.buffer = bytearray(capacity)
        self.free_blocks = [(0, capacity)]
        self.allocations: dict[int, Block] = {}
        self.probes = 0
        self._next_id = 0

    def allocate(self, size: int) -> Block | None:
        if size < 1:
            raise ValueError("Allocation size must be positive")
        if len(self.allocations) >= 4096:
            raise ValueError("Controlled buffer allocation limit reached")
        for index, (offset, length) in enumerate(self.free_blocks):
            self.probes += 1
            if length < size:
                continue
            self._next_id += 1
            block = Block(self._next_id, offset, size)
            self.allocations[block.identity] = block
            self.free_blocks[index : index + 1] = (
                [(offset + size, length - size)] if length > size else []
            )
            return block
        return None

    def write(self, identity: int, data: bytes) -> None:
        block = self.allocations[identity]
        if len(data) > block.size:
            raise ValueError("Write exceeds the allocated block")
        self.buffer[block.offset : block.offset + len(data)] = data

    def read(self, identity: int) -> bytes:
        block = self.allocations[identity]
        return bytes(self.buffer[block.offset : block.offset + block.size])

    def release(self, identity: int) -> None:
        block = self.allocations.pop(identity)
        self.free_blocks.append((block.offset, block.size))
        merged: list[tuple[int, int]] = []
        for offset, length in sorted(self.free_blocks):
            if merged and merged[-1][0] + merged[-1][1] == offset:
                previous, size = merged.pop()
                merged.append((previous, size + length))
            else:
                merged.append((offset, length))
        self.free_blocks = merged

    def snapshot(self) -> dict[str, object]:
        free = sum(size for _, size in self.free_blocks)
        largest = max((size for _, size in self.free_blocks), default=0)
        return {
            "model": "application-controlled-buffer",
            "capacity_bytes": len(self.buffer),
            "free_bytes": free,
            "largest_free_bytes": largest,
            "external_fragmentation": 1 - largest / free if free else 0.0,
            "free_blocks": list(self.free_blocks),
            "allocation_count": len(self.allocations),
            "metadata_entries": len(self.free_blocks) + len(self.allocations),
            "search_probes": self.probes,
        }
