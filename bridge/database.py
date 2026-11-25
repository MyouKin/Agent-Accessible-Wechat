"""Reserved integration point for a legally accessible read-only DatabaseSource.

Future implementations conform to models.MessageSource and should use stable original
IDs. The present synchronizer consumes SnapshotSource until a cursor backend is added.
"""

from .models import MessageSource

__all__ = ["MessageSource"]
