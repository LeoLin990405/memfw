"""Synthetic R0 retrieval baseline.

The harness builds its own questions and vaults. It does not read a live vault,
call a model, or accept an endpoint from a request.
"""

GENERATOR_VERSION = 1

__all__ = ["GENERATOR_VERSION"]
