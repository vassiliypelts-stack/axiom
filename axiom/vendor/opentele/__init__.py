# td (TDesktop/tdata) требует PyQt5 — не всем нужен (например, .session-only
# сценарии). Импортируем лениво, чтобы отсутствие/битый PyQt5 не ломал остальное.
try:
    from . import td
except ImportError:
    pass
from . import tl
