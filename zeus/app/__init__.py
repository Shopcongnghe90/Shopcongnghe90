"""App factory FastAPI. Integrator nối router của A (Control/Worker API) và D (Workbench, hooks)."""

from zeus.app.main import create_app

__all__ = ["create_app"]
