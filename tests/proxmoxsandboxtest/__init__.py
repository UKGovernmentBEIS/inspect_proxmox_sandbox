import logging

# httpcore and httpx emit extremely verbose DEBUG logs (every TCP connect,
# TLS handshake, header send/receive, body send/receive, etc.) that drown
# out application-level debug output.  Rather than disabling DEBUG globally,
# we raise the level on just these loggers so our own DEBUG messages remain
# visible.  Doing this in the package __init__ ensures it runs before
# conftest.py imports any module that pulls in httpx.
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
