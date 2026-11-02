#!/usr/bin/env python3
"""Dynamic inventory for the qemu-tool test cluster.

The topology is already described once, in cluster/env/nixl-mori, and
cluster/scripts/cluster.sh reads it. Deriving the inventory from the same file
rather than writing a static hosts file is the point: VM_COUNT,
VM_SSH_PORT_BASE and ERNIC_GUEST_SUBNET change often enough that a second copy
would be wrong within a session, and wrong in a way that targets the wrong
guests rather than failing.

Environment overrides win over the file, matching envget() in cluster.sh, so a
second cluster can be addressed without editing anything.
"""

import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = Path(os.environ.get("CLUSTER_ENV_FILE", REPO_ROOT / "cluster/env/nixl-mori"))

# Same default as cluster.sh. This is the key baked into VM_BACKING_IMAGE, not
# a key of ours -- see the comment on SSH_KEY there.
DEFAULT_SSH_KEY = "/data/qemu-tool-stebates/artifacts/20261030/id_rsa"


def envget(key: str, default: str = "") -> str:
    val = os.environ.get(key)
    if val:
        return val
    if not ENV_FILE.is_file():
        return default
    found = default
    for line in ENV_FILE.read_text().splitlines():
        m = re.match(rf"^{re.escape(key)}=(.*)$", line)
        if m:
            found = m.group(1).strip()
    return found


def build() -> dict:
    count = int(envget("VM_COUNT", "2"))
    base = int(envget("VM_SSH_PORT_BASE", "2400"))
    subnet = envget("ERNIC_GUEST_SUBNET", "192.168.220")
    key = os.environ.get("SSH_KEY", DEFAULT_SSH_KEY)

    hostvars = {}
    for n in range(1, count + 1):
        hostvars[f"vm{n}"] = {
            "ansible_host": "localhost",
            "ansible_port": base + n - 1,
            "ansible_user": "ubuntu",
            "ansible_ssh_private_key_file": key,
            "ansible_ssh_common_args":
                "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                "-o LogLevel=ERROR",
            "ansible_python_interpreter": "/usr/bin/python3",
            "vm_index": n,
            # Guests get .1n, so vm1 is .11. Not .1: the mesh manager claims
            # that as its DHCP server_ip and intercepts ARP for it.
            "ernic_ip": f"{subnet}.1{n}",
        }

    return {
        "cluster": {"hosts": sorted(hostvars, key=lambda h: int(h[2:]))},
        "_meta": {"hostvars": hostvars},
        "all": {
            "children": ["cluster"],
            "vars": {
                "ernic_subnet": subnet,
                "rocjitsu_image": envget("ROCJITSU_IMAGE", ""),
            },
        },
    }


if __name__ == "__main__":
    if "--host" in sys.argv:
        print(json.dumps({}))
    else:
        print(json.dumps(build(), indent=2))
