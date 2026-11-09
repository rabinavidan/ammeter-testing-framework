from socket import AF_INET, SOCK_STREAM, socket
from typing import Optional

# Each reply is one line: the reading as text, ended by a newline. Over a real link a reply can
# arrive in several pieces, so the client reads until the newline instead of trusting one recv().
TERMINATOR = b"\n"
OVER_RANGE = "OL"     # what a meter shows when the current is beyond its range


class TruncatedReplyError(ValueError):
    """The connection closed after part of a reply, before its terminator."""


class OverRangeError(ValueError):
    """The meter reported over-range ("OL") instead of a reading."""


def parse_reply(data: bytes) -> Optional[float]:
    """
    What a complete reply means, separated from the socket so the protocol rules can be tested alone:
    nothing -> None; no terminator -> TruncatedReplyError; "OL" -> OverRangeError; otherwise the
    number (ValueError / UnicodeDecodeError if it isn't one). Text after the terminator is ignored.
    """
    if not data:
        return None
    if TERMINATOR not in data:
        raise TruncatedReplyError(f"reply ended before its terminator: {data!r}")
    text = data.split(TERMINATOR, 1)[0].decode('utf-8').strip()
    if text == OVER_RANGE:
        raise OverRangeError("meter reported over-range (OL)")
    return float(text)


def request_current_from_ammeter(port: int, command: bytes, timeout: Optional[float] = None,
                                 host: str = "localhost") -> Optional[float]:
    """
    Send `command` and return the reading in amps, or None if the meter closed without replying.

    Raises TimeoutError if a reply doesn't arrive within `timeout` seconds (per receive),
    TruncatedReplyError if the reply is cut off, OverRangeError for "OL", and ValueError
    (or UnicodeDecodeError) if the reply isn't a number.
    """
    with socket(AF_INET, SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect((host, port))
        s.sendall(command)
        data = b""
        while TERMINATOR not in data:
            chunk = s.recv(1024)
            if not chunk:
                break
            data += chunk
        if not data:
            print("No data received.")
            return None
        try:
            value = parse_reply(data)
        except ValueError:
            # repr() is plain ASCII, so printing an invalid reply can't fail on any console encoding
            print(f"Invalid reply from port {port}: {data!r}")
            raise
        if value is not None:
            print(f"Received current measurement from port {port}: {value} A")
        return value
