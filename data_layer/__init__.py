"""SEC filing data for the agents: cached, chunked 10-K / 10-Q sections.

    from data_layer import load_sections, list_available
    chunks = load_sections("AXP", "10-K", "debt")
"""

from data_layer.extract import SECTIONS
from data_layer.store import list_available, load_sections

__all__ = ["SECTIONS", "list_available", "load_sections"]
