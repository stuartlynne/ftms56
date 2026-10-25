import sys
import threading, select, socket, time
import logging
logger = logging.getLogger(__name__)

class SocketMonitor(threading.Thread):
    """
    Monitor a socket to ensure that the connection is alive. Signals
    a shutdown to the provided dataQ if the socket is no longer alive.
    """
    def __init__(self, sock, dataQ, poll_interval=1.0, name=None):
        super().__init__(name=name or "SocketMonitor", daemon=True)
        logger.debug("%s: initializing with socket=%r dataQ=%r poll_interval=%s", self.name, sock, dataQ, poll_interval)
        self._sock = sock
        self.dataQ = dataQ
        self._poll_interval = poll_interval

    def _socket_alive(self) -> bool:
        try:
            fileno = self._sock.fileno()
            if fileno < 0:
                logger.debug("%s: socket already closed locally", self.name)
                return False
            #logger.debug("%s: checking if socket %r is alive", self.name, self._sock)
            r, _, _ = select.select([self._sock], [], [], 0)
            if not r:
                #logger.debug("%s: socket %r not readable (likely OK)", self.name, self._sock)
                return True                     # nothing readable: likely OK
            # Readable: if peer closed, peek returns b''; if error, OSError.
            data = self._sock.recv(1, socket.MSG_PEEK)
            #logger.debug("%s: peeked data: %r", self.name, data)
            return bool(data)
        except (BlockingIOError, InterruptedError):
            logger.debug("%s: socket is non-blocking, no data to peek", self.name)
            return True
        except (OSError, ValueError):
            logger.debug("%s: socket error/invalid fd, likely dead", self.name)
            return False

    def run(self):
        logger.debug("%s: starting monitor for socket %r", self.name, self._sock)
        # loop until someone asks us to shut down, or socket dies
        while not self.dataQ.shutdown_requested():
            if not self._socket_alive():
                logger.info("%s: socket dead, closing", self.name)
                # signal everyone to unwind
                self.dataQ.set_closed()
                return
            #logger.debug("%s: socket alive, continuing to monitor", self.name)
            time.sleep(self._poll_interval)
