"""Hard stop: shut a stalled socket down even when the server keeps it open."""

import http.client
import socket
import threading
from typing import Self

from .constants import SITE_HARD
from .timing import Deadline


class FetchDeadline:
    """Per-thread hard-stop timer for the connection currently in progress."""

    _local = threading.local()

    @classmethod
    def begin_window(cls, deadline: Deadline) -> None:
        """Start the window that ends at ``deadline`` for this thread's fetch."""
        cls._local.deadline = deadline

    @classmethod
    def end_window(cls) -> None:
        """Cancel the timer and clear this thread's deadline."""
        cls.cancel_timer()
        cls._local.deadline = None

    @classmethod
    def current(cls) -> Deadline | None:
        return getattr(cls._local, "deadline", None)

    @classmethod
    def arm(cls, connection: http.client.HTTPConnection) -> None:
        """Schedule a socket shutdown that wakes a blocked read when time is up.

        The socket is captured now because urllib sends ``Connection: close``,
        so the connection object drops its reference once the response starts.
        The fetching thread closes the socket itself; closing it here could
        wait on the socket's buffer lock.
        """
        sock = connection.sock

        def shut_down() -> None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        cls.cancel_timer()
        deadline = cls.current()
        delay = max(0.1, deadline.remaining()) if deadline else SITE_HARD
        timer = threading.Timer(delay, shut_down)
        timer.daemon = True
        timer.start()
        cls._local.timer = timer

    @classmethod
    def cancel_timer(cls) -> None:
        """Cancel this thread's pending hard-stop timer, if any."""
        timer = getattr(cls._local, "timer", None)
        if timer is not None:
            timer.cancel()
            cls._local.timer = None


class FetchWindow:
    """Context manager that bounds one fetch with :class:`FetchDeadline`."""

    def __init__(self, deadline: Deadline):
        self._deadline = deadline

    def __enter__(self) -> Self:
        FetchDeadline.begin_window(self._deadline)
        return self

    def __exit__(self, *exc_info) -> None:
        FetchDeadline.end_window()
