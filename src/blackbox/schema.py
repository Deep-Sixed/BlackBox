"""Current schema; historical definitions live in migrations."""

from .migrations.v001 import APPLICATION_ID
from .migrations.v002 import DDL, TABLES, VERSION

__all__ = ["APPLICATION_ID", "DDL", "TABLES", "VERSION"]
