"""Verify owned process groups only at cancellation and shutdown boundaries."""

import os

import psutil


class ProcessGroup:
    """Retain live member identities for one owned process group during cleanup.

    Capture before signalling the owner, while its children still belong to the
    original session. Process ancestry is not required. A retained member can
    authorize group delivery after the leader exits, including delivery to new
    members of that same group. Once all retained members disappear or leave,
    fail closed instead of adopting a potentially reused group/session number.
    No process enumeration or identity checks run during interactive input.
    """

    def __init__(self, group: int, session: int):
        """Snapshot only this group's members, without using process_iter's cache as identity."""
        self.group = group
        self.session = session
        self.members: list[psutil.Process] = []
        if group <= 0 or session <= 0 or group == os.getpgrp():
            return
        try:
            for candidate in psutil.process_iter():
                try:
                    if (
                        os.getpgid(candidate.pid) != group
                        or os.getsid(candidate.pid) != session
                    ):
                        continue
                    # process_iter caches Process instances across calls. A fresh
                    # object records the current PID/create-time identity instead.
                    member = psutil.Process(candidate.pid)
                    member.create_time()
                    if self._matches(member):
                        self.members.append(member)
                except (OSError, psutil.Error):
                    continue
        except (OSError, psutil.Error):
            # Partial visibility may still yield a verified surviving member.
            pass

    def _matches(self, member: psutil.Process) -> bool:
        """Recheck identity and membership, excluding zombies that cannot be signalled."""
        try:
            return (
                member.is_running()
                and os.getpgid(member.pid) == self.group
                and os.getsid(member.pid) == self.session
                and member.status() != psutil.STATUS_ZOMBIE
                and member.is_running()
            )
        except (OSError, psutil.Error):
            return False

    def send(self, signum: int) -> bool:
        """Signal the group only while a captured live member still verifies ownership."""
        if self.group <= 0 or self.group == os.getpgrp():
            return False
        for member in self.members:
            if self._matches(member):
                try:
                    os.killpg(self.group, signum)
                    return True
                except OSError:
                    return False
        return False
