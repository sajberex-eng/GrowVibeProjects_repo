import threading

_state = threading.local()


def set_context(user, ip):
    _state.user = user
    _state.ip = ip


def clear_context():
    _state.user = None
    _state.ip = None


def current_user():
    return getattr(_state, "user", None)


def current_ip():
    return getattr(_state, "ip", None)
