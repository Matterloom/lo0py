"""Handling of Caddy proxies."""
import json
import os
from pathlib import Path

import requests


class ForwardManager:
    """Class for setting up/tearing down forwarding."""

    def __init__(self, address: str, unix_socket_path: Path) -> None:
        """Initialize ForwardManager."""
        self._address = address
        self._unix_socket_path = unix_socket_path

    def start(self) -> None:
        """Begin forwarding.

        Can raise requests.exceptions.Timeout and requests.RequestException.
        """
        if bool(os.environ.get("LO0APP_TESTING", False)):
            return

        post_json = {
            "@id": f"lo0app_sub_{self._address}",
            "handle": [
                {
                    "handler": "reverse_proxy",
                    "headers": {
                        "response": {
                            "add": {"Strict-Transport-Security": ["max-age=31536000;"]},
                            "delete": ["server"],
                        },
                    },
                    "upstreams": [
                        {"dial": f"unix/{self._unix_socket_path.as_posix()}"},
                    ],
                },
            ],
            "match": [{"host": [self._address]}],
        }

        url = "http://localhost:2019/id/lo0app_handler/routes/0"
        headers = {"Accept": "text/plain", "Content-Type": "application/json"}

        requests.put(url, headers=headers, data=json.dumps(post_json), timeout=3)

    def stop(self) -> None:
        """Stop forwarding."""
        if bool(os.environ.get("LO0APP_TESTING", False)):
            return
        requests.delete(
            f"http://localhost:2019/id/lo0app_sub_{self._address}",
            timeout=3,
        )
