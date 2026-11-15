"""Hold a native editor widget until its matching prompt popup is closed."""

import argparse
import json
import socket

MAX_REPLY = 64


def main():
    """Use a private control socket; never read or write command stdin."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--serial", required=True, type=int)
    args = parser.parse_args()
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(args.socket)
            payload = {"nonce": args.nonce, "serial": args.serial}
            connection.sendall(json.dumps(payload).encode("ascii") + b"\n")
            answer = bytearray()
            while b"\n" not in answer and len(answer) < MAX_REPLY:
                data = connection.recv(MAX_REPLY - len(answer))
                if not data:
                    break
                answer.extend(data)
            return 0 if answer in (b"done\n", b"cancel\n") else 1
    except OSError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
