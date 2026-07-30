"""통합 웹의 대메뉴(모듈) 연동 계층.

각 대메뉴는 담당 WAS 를 하나씩 갖고, 통합 웹은 BFF 로서 그 WAS 를 서버에서
호출한다. 브라우저는 통합 웹만 바라본다.
"""

from .client import ModuleClient, ModuleResponse
from .panels import PanelAggregator, normalize_panel, register_local_panel
from .registry import ModuleConfigError, ModuleDefinition, ModuleRegistry
from .routes import create_modules_blueprint
from .tokens import issue_token, mask_token, verify_token

__all__ = [
    "ModuleClient",
    "ModuleConfigError",
    "ModuleDefinition",
    "ModuleRegistry",
    "ModuleResponse",
    "PanelAggregator",
    "create_modules_blueprint",
    "issue_token",
    "mask_token",
    "normalize_panel",
    "register_local_panel",
    "verify_token",
]
