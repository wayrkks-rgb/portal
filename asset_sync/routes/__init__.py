from .admin import create_admin_blueprint
from .collection import create_collection_blueprint
from .core import create_core_blueprint
from .itsm import create_itsm_blueprint

__all__ = [
    "create_admin_blueprint",
    "create_collection_blueprint",
    "create_core_blueprint",
    "create_itsm_blueprint",
]
