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

def default_ssh_key(images_dir: str, backing_image: str) -> str:
    """Where `cluster.sh images` puts the fleet key, derived the same way.

    The key is baked into VM_BACKING_IMAGE rather than being one of ours, so
    it moves when that pin moves. Deriving the path from the pin keeps this in
    step automatically; a literal path would go on resolving after a bump --
    to the previous image's key -- so the failure would be every guest
    refusing the key rather than a missing file.
    """
    stamp = backing_image.rsplit(":", 1)[-1].split(".", 1)[0] if backing_image else ""
    return str(Path(images_dir).parent / "artifacts" / stamp / "id_rsa")


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
    key = os.environ.get("SSH_KEY") or default_ssh_key(
        envget("VM_IMAGES_DIR", "/data/qemu-tool-stebates/images"),
        envget("VM_BACKING_IMAGE", ""),
    )

    per_vm = int(envget("ERNIC_PER_VM", "1"))
    mac_prefix = envget("ERNIC_MAC_PREFIX", "72:6f:63:6e")

    hostvars = {}
    for n in range(1, count + 1):
        # Addresses and MACs are keyed on the GLOBAL nic ordinal, not the VM
        # index, so at ERNIC_PER_VM=2 vm2's first rail is .13 rather than .12.
        # Getting this wrong points a benchmark at another guest's rail, which
        # works -- it is a real address on a real fabric -- and quietly
        # measures the wrong path.
        rails = []
        for r in range(1, per_vm + 1):
            g = (n - 1) * per_vm + r
            rails.append({
                "ip": f"{subnet}.{10 + g}",
                # Matching netplan on the MAC rather than the interface name:
                # adding GPUs shifts the PCI slots, and with them enpXs0np0.
                "mac": f"{mac_prefix}:00:{g:02x}",
                "rail": r,
            })

        hostvars[f"vm{n}"] = {}
        hostvars[f"vm{n}"].update({
            "ansible_host": "localhost",
            "ansible_port": base + n - 1,
            "ansible_user": "ubuntu",
            "ansible_ssh_private_key_file": key,
            "ansible_ssh_common_args":
                "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                "-o LogLevel=ERROR",
            "ansible_python_interpreter": "/usr/bin/python3",
            "vm_index": n,
            "ernic_rails": rails,
            # The first rail, for everything that wants one address. Not .1:
            # the mesh manager claims that as its DHCP server_ip and
            # intercepts ARP for it.
            "ernic_ip": rails[0]["ip"],
        })

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
