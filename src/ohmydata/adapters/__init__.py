"""Optional dataframe representation adapters."""

from .polars import pandas_to_polars, polars_to_pandas

__all__ = ["pandas_to_polars", "polars_to_pandas"]
