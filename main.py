"""本地启动入口：``python main.py`` 或 ``uv run python main.py``。"""

from __future__ import annotations

import uvicorn

from app.config import Settings
from app.http_api import create_app
from app.service import UploadService


def main() -> None:
    settings = Settings.from_env()
    service = UploadService(settings)
    service.start()
    app = create_app(service)
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
