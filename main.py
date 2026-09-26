"""本地启动入口：``uv run python main.py`` 或 ``uv run uvicorn main:app``。"""

import logging

import uvicorn

from gateway.app import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
