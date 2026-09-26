"""最小冒烟测试，保证骨架可导入可构建。"""

from __future__ import annotations


def test_imports():
    import app.config  # noqa: F401
    import app.errors  # noqa: F401
    import app.http_api  # noqa: F401
    import app.models  # noqa: F401
    import app.service  # noqa: F401
    import app.storage  # noqa: F401
    import app.sweeper  # noqa: F401
