"""Classic cache/prefetch controls for the user's gem5 v25.1 setup.

Calls the stock hierarchy's wiring, then changes only selected prefetchers
before m5.instantiate(). L1I prefetching keeps its stock configuration.
"""

from m5.params import NULL
from gem5.components.cachehierarchies.classic.no_cache import NoCache
from gem5.components.cachehierarchies.classic.private_l1_cache_hierarchy import (
    PrivateL1CacheHierarchy,
)
from gem5.components.cachehierarchies.classic.private_l1_private_l2_cache_hierarchy import (
    PrivateL1PrivateL2CacheHierarchy,
)


def _cache_at(hierarchy, level, index):
    # v25.1 uses CacheNode and attaches the cache under this exact name.
    name = f"{level}-cache-{index}"
    try:
        return getattr(hierarchy, name)
    except AttributeError:
        # PrivateL1CacheHierarchy and older private-L2 implementations use lists.
        legacy = getattr(hierarchy, f"{level}caches", None)
        if legacy is not None:
            return legacy[index]
        raise RuntimeError(f"Cannot locate {name}; check this gem5 version's cache hierarchy")


class ControlledL1L2(PrivateL1PrivateL2CacheHierarchy):
    def __init__(self, *, l1d_prefetch=True, l2_prefetch=True):
        super().__init__(l1i_size="32KiB", l1d_size="32KiB", l2_size="256KiB")
        self._enable_l1d_prefetch = l1d_prefetch
        self._enable_l2_prefetch = l2_prefetch

    def incorporate_cache(self, board):
        super().incorporate_cache(board)
        for index in range(board.get_processor().get_num_cores()):
            if not self._enable_l1d_prefetch:
                _cache_at(self, "l1d", index).prefetcher = NULL
            if not self._enable_l2_prefetch:
                _cache_at(self, "l2", index).prefetcher = NULL


class ControlledL1(PrivateL1CacheHierarchy):
    def __init__(self, *, l1d_prefetch=True):
        super().__init__(l1i_size="32KiB", l1d_size="32KiB")
        self._enable_l1d_prefetch = l1d_prefetch

    def incorporate_cache(self, board):
        super().incorporate_cache(board)
        for index in range(board.get_processor().get_num_cores()):
            if not self._enable_l1d_prefetch:
                _cache_at(self, "l1d", index).prefetcher = NULL


def make_cache_hierarchy(mode, l1d_prefetch, l2_prefetch):
    if mode == "none":
        return NoCache()
    if mode == "l1":
        return ControlledL1(l1d_prefetch=l1d_prefetch)
    if mode == "l1l2":
        return ControlledL1L2(l1d_prefetch=l1d_prefetch, l2_prefetch=l2_prefetch)
    raise ValueError(f"Unknown cache mode: {mode}")
