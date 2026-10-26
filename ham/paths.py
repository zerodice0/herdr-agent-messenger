"""Canonical plugin root and shared executable-path trust policy."""
import grp
import os
from pathlib import Path
import pwd
import stat
import sys

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def trusted(path: Path) -> bool:
    """Require root/current-user ownership and no untrusted writer on the path."""
    uid, gid = os.getuid(), os.getgid()
    writable_groups = set()
    if sys.platform == "darwin":
        try:
            writable_groups.add(grp.getgrnam("admin").gr_gid)
        except KeyError:
            pass
    private_group = None
    try:
        resolved = path.resolve(strict=True)
        for item in (resolved, *resolved.parents):
            info = item.stat()
            if info.st_uid not in {0, uid} or info.st_mode & stat.S_IWOTH:
                return False
            if info.st_mode & stat.S_IWGRP and info.st_gid not in writable_groups:
                if info.st_gid != gid:
                    return False
                if private_group is None:
                    group = grp.getgrgid(gid)
                    user = pwd.getpwuid(uid)
                    private_group = (group.gr_name == user.pw_name and not group.gr_mem and
                                     not any(entry.pw_name != user.pw_name and entry.pw_gid == gid
                                             for entry in pwd.getpwall()))
                if not private_group:
                    return False
        return True
    except (OSError, KeyError):
        return False
