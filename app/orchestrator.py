"""OVS network orchestration script.

Basic Script that depends on having ovs-vsctl pre-installed in the system.
Need to ensure that the dockerr.socket is mounted else the program must close.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import sys
import time
import traceback
from functools import cache
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess, run
from typing import Any, TypedDict

_LOGGER = logging.getLogger()
_LOGGING_LEVEL = (
    logging.INFO if os.environ.get("DEBUG", "no") == "no" else logging.DEBUG
)
_LOGGER.setLevel(_LOGGING_LEVEL)

_HANDLER = logging.StreamHandler(sys.stdout)
_HANDLER.setLevel(_LOGGING_LEVEL)

_FORMAT = "[%(asctime)s] [%(levelname)s] %(funcName)s:: %(message)s"
_FORMATTER = logging.Formatter(_FORMAT)
_HANDLER.setFormatter(_FORMATTER)
_LOGGER.addHandler(_HANDLER)

_DB_JSON_PATH = Path("/tmp/db.json")  # noqa: S108

_MAX_FAIL_COUNT = 2

_DOCKER_SOCKET = Path("/var/run/docker.sock")
_USE_LINUX_BRIDGE = os.environ.get("USE_LINUX_BRIDGE", False)


class ParentInfo(TypedDict, total=False):
    """Schema for OVS/Linux Bridge parent interface details."""

    iface: str  # Optional, parent interface OVS speaks to
    native: str  # Optional, native VLAN for untagged packets
    trunk: str  # Optional, Comma separated VLAN ids


class BridgeInfo(TypedDict, total=False):
    """Schema for OVS/Linux Bridge details."""

    parents: list[ParentInfo]
    iprange: str  # Optional, subnet with prefix
    ip6range: str  # Optional, subnet with prefix
    ipaddress: str  # Optional, IPv4 address
    ip6address: str  # Optional, IPv6 address


class ContainerInfo(TypedDict, total=False):
    """Schema for Container Interface Bridge details."""

    iface: str  # Interface name inside of container.
    bridge: str  # Bridge name interface needs to be part of
    vlan: str  # Optional, VLAN ID interface should be part of
    trunk: str  # Optional, Comma separated VLAN ids
    ipaddress: str  # Optional, IPv4 address
    ip6address: str  # Optional, IPv6 address
    gateway: str  # Optional, IPv4 gateway address
    gateway6: str  # Optional, IPv6 gateway address
    macaddress: str  # Optional, MAC address for the interface


@cache
def _get_db() -> dict[str, Any]:
    """Get the OVS database cache.

    :return: The OVS database cache.
    :rtype: dict[str, Any]
    """
    return json.load(_DB_JSON_PATH.open(encoding="utf-8"))


def _run_command(command: str, check: bool = True) -> CompletedProcess[str]:
    """Run a command using subprocess and capture the output.

    :param command: The command to run.
    :type command: str
    :param check: flag to raise an exception on command fail.
    :type check: bool
    :return: The captured output of the command as a string.
    :rtype: CompletedProcess[str]
    :raises CalledProcessError: If the command execution fails.
    """
    try:
        return run(command.split(), check=check, capture_output=True, text=True)
    except CalledProcessError as exc:
        _LOGGER.exception("Subprocess error:\nCommand failed: %s", exc.cmd)
        stderr_output = exc.stderr if exc.stderr else None
        _LOGGER.exception("Command stderr output:\n%s", stderr_output)
        raise


def _get_interface_ip(interface: str) -> list[str | None]:
    """Get the IP address of a network interface.

    :param interface: The name of the network interface.
    :type interface: str
    :return: The IP address of the interface, or None if not found.
    :rtype: list[str | None]
    """
    try:
        command = ["ip", "-o", "addr", "show", interface]
        result = run(command, check=True, capture_output=True, text=True)
        return re.findall(r"inet\d*\s+(\S+)", result.stdout.strip())
    except CalledProcessError as exc:
        _LOGGER.exception("Subprocess error:\nCommand failed: %s", exc.cmd)
        stderr_output = exc.stderr.strip()
        _LOGGER.exception("Command stderr output:\n%s", stderr_output)
    return []


def _hash_string(string: str) -> str:
    """Hashes a string using the SHA-256 algorithm.

    Returns the first 8 bits as an 8-bit string.

    :param string: The input string to be hashed.
    :type string: str
    :return: The first 10 bits of the hash as an 8-bit string.
    :rtype: str
    """
    # Hash the input string using SHA-256 algorithm and convert
    # the first 8 characters (4 bytes) of the hash to an 8-bit string
    return hashlib.sha256(string.encode()).hexdigest()[:8]


def _veth_exists(veth_end: str) -> bool:
    """Check if a veth or veth pair exists.

    :param veth_end: Name of the veth or veth pair end to check.
    :type veth_end: str
    :return: True if the veth or veth pair exists, False otherwise.
    :rtype: bool
    """
    # Run the ip link show command and check if the veth
    # or veth pair end is present in the output
    result = _run_command(f"ip link show {veth_end}", check=False)
    return result.returncode == 0


def _add_iface_to_ovs_bridge(bridge_name: str, parent_info: ParentInfo) -> None:
    # Check if the interface already exists
    parent = parent_info.get("iface")
    db_cache = _get_db().get(bridge_name, {})
    parent_cache = db_cache.setdefault(parent, {})

    check = _run_command(f"ovs-vsctl port-to-br {parent}", check=False)
    if check.stdout.strip() != bridge_name:
        _LOGGER.debug("Parent %s not part of OVS bridge %s", parent, bridge_name)
        _run_command(f"ovs-vsctl --if-exists del-port {parent}", check=False)

        cmd = f"ovs-vsctl --may-exist add-port {bridge_name} {parent}"

        if trunk := parent_info.get("trunk"):
            parent_cache["trunk"] = trunk
            cmd = f"{cmd} trunk={trunk}"

        if native_vlan := parent_info.get("native"):
            parent_cache["native"] = native_vlan
            cmd = f"{cmd} vlan_mode=native-untagged tag={native_vlan}"

        _run_command(cmd)
        _LOGGER.debug("parent %s up for OVS bridge %s", parent, bridge_name)


def _add_iface_to_linux_bridge(bridge_name: str, parent_info: ParentInfo) -> None:
    parent = parent_info.get("iface", "foo")
    db_cache = _get_db().get(bridge_name, {})
    parent_cache = db_cache.setdefault(parent, {})

    check = _run_command(f"ip -o link show master {bridge_name}", check=False)
    if parent not in check.stdout:
        _LOGGER.debug("Parent %s not part of Linux bridge %s", parent, bridge_name)
        _run_command(f"ip link set dev {parent} nomaster")

        _run_command(f"brctl addif {bridge_name} {parent}")
        _run_command(f"ip link set {bridge_name} type bridge vlan_filtering 1")

    if trunk := parent_info.get("trunk"):
        parent_cache["trunk"] = trunk
        _run_command(f"bridge vlan delete dev {parent} vid 1")
        for vid in trunk.split(","):
            _run_command(f"bridge vlan add dev {parent} vid {vid}")

    if native_vlan := parent_info.get("native"):
        parent_cache["native"] = native_vlan
        _run_command(f"bridge vlan delete dev {parent} vid 1")
        _run_command(f"bridge vlan add dev {parent} vid {native_vlan} pvid untagged")


def _add_iface_to_bridge(bridge_name: str, parent_info: ParentInfo) -> None:  # noqa: C901
    """Add a network interface to an OVS bridge.

    :param bridge_name: The name of the OVS bridge.
    :type bridge_name: str
    :param parent_info: The OVS/Linux bridge parent details.
    :type parent_info: ParentInfo
    :raises ValueError: If more than usb-parent exist for provided ID.
    """
    db_cache = _get_db().get(bridge_name, {})

    if (parent := parent_info.get("iface")) is None:
        _LOGGER.debug("Invalid Entry: %s \nSkipping ..", str(parent_info))
        return

    if "usb:" in parent:
        usb_port = parent.split(":")[-1]
        devs = _run_command("ls -l /sys/class/net", check=False)
        usb_info = [dev for dev in devs.stdout.splitlines() if usb_port in dev]
        if len(usb_info) > 1:
            msg = f"Identified more than one interface for usb bus: {usb_port}"
            raise ValueError(msg)
        parent = usb_info[0].split()[8]

    parent_cache = db_cache.setdefault(parent, {})
    _LOGGER.debug("Trying to bring up parent %s for bridge %s", parent, bridge_name)
    _run_command(f"ip link set {parent} up")

    if _USE_LINUX_BRIDGE:
        _add_iface_to_linux_bridge(bridge_name, parent_info)
    else:
        _add_iface_to_ovs_bridge(bridge_name, parent_info)

    for key, opt in [
        ("trunk", "trunk={}"),
        ("native", "vlan_mode=native-untagged tag={}"),
    ]:
        if (value := parent_info.get(key)) != parent_cache.get(key):
            _LOGGER.info("New %s %s setting applied for parent %s", key, value, parent)
            parent_cache[key] = value

            if not value:
                continue

            if _USE_LINUX_BRIDGE:
                _run_command(f"ip link set {bridge_name} type bridge vlan_filtering 1")
                _run_command(f"bridge vlan delete dev {parent} vid 1")
                for vid in str(value).split(","):
                    cmd = f"bridge vlan add dev {parent} vid {vid}"
                    if key == "native":
                        cmd += " pvid untagged"
                    _run_command(cmd)
            else:
                _run_command(f"ovs-vsctl set port {parent} {opt.format(value)}")


def init_bridge(bridge_name: str, info: BridgeInfo) -> None:  # noqa: C901
    """Create an OVS/Linux bridge if it does not exist.

    If a parent interface is provided as part of the OVS bridge info,
    then it shall be a member of the bridge.

    The trunk and native VLAN details are associated to the parent port in OVS.

    :param bridge_name: OVS bridge name
    :type bridge_name: str
    :param info: OVS/Linux bridge details
    :type info: BridgeInfo
    :raises ValueError: If IP address is already allocated/incorrect.
    """
    _LOGGER.debug("################## OVS BRIDGES #####################")
    db_cache = _get_db().setdefault(bridge_name, {})

    cmd, check = f"ovs-vsctl --may-exist add-br {bridge_name}", True
    if _USE_LINUX_BRIDGE:
        cmd, check = f"brctl addbr {bridge_name}", False
    _run_command(cmd, check)
    _run_command(f"ip link set {bridge_name} up")

    # Update bridge specific IP address range and Host details
    for range_key, ip_addr in (
        ("iprange", info.get("ipaddress")),
        ("ip6range", info.get("ip6address")),
    ):
        if (ip_range := info.get(range_key)) != db_cache.get(range_key):
            # Since IP range in cache is getting updated,
            # remove all previous host reservations.

            _LOGGER.debug("Updating IP range for %s to %s", bridge_name, ip_range)
            db_cache[range_key] = ip_range
            db_cache[f"{range_key}_hosts"] = {}

        hosts = db_cache.setdefault(f"{range_key}_hosts", {})
        family = "-4" if range_key == "iprange" else "-6"

        if not ip_addr:
            # If ip_addr is not requested, simply flush and continue
            _LOGGER.debug("Flusing IP address for %s", bridge_name)

            hosts.pop(bridge_name, None)
            _run_command(f"ip {family} addr flush dev {bridge_name}", check=False)
            continue

        set_ip, cache_changed = False, False

        if ip_addr != hosts.get(bridge_name):
            # If ipaddress has changed, update the the cache.
            # Will check if the new IP is not in conflict with any other host
            # before assigning to the bridge.

            hosts.pop(bridge_name, None)
            _run_command(f"ip {family} addr flush dev {bridge_name}", check=False)
            set_ip, cache_changed = True, True

        elif ip_addr not in _get_interface_ip(bridge_name):
            # If for some reason, ipaddress on the bridge interface
            # is lost, re-add the ip again.
            set_ip = True

        if set_ip:
            if cache_changed and ip_addr in hosts.values():
                msg_set_ip_err = (
                    f"IP {ip_addr} already allocated to someone else. ",
                    f"Cannot assign request address to bridge {bridge_name}",
                )
                raise ValueError(msg_set_ip_err)

            if ipaddress.ip_interface(ip_addr) not in ipaddress.ip_network(
                str(ip_range)
            ):
                # Ensure that IP address provided by user is always
                # part of the same range as that being maintained by the bridge.
                msg = f"{ip_addr} does not fall under the range {ip_range}"
                raise ValueError(msg)

            hosts[bridge_name] = ip_addr
            _run_command(f"ip addr add {ip_addr} dev {bridge_name}")
            _LOGGER.info("Updated IP address for %s to %s", bridge_name, ip_addr)

    # Add parent interfaces
    for parent_info in info.get("parents", []):
        _add_iface_to_bridge(bridge_name=bridge_name, parent_info=parent_info)


def create_veth_pair(on_bridge: str, vlan_map: str) -> None:
    """Create a veth pair and attach it to OVS bridges.

    Create a veth pair and attach each end of the pair to the OVS bridge with
    the name ```on_bridge``` .

    Set VLAN tags on each end based on the `vlan_map`.

    The function follows the following rules:
    - If the veth pair already exists, it is assumed that both ends are created.
    - The veth endpoints are attached to the specific bridges.
    - VLAN tags are set on the bridge interfaces.
    - The external_ids are set to track the VLAN translation.

    :param on_bridge: OVS bridge to attach the first veth end.
    :type on_bridge: str
    :param vlan_map: VLAN mapping in the format "source_vlan:dest_vlan".
    :type vlan_map: str
    :raises ValueError: If the veth pair is already attached to different bridges.
    """
    _LOGGER.debug("################## VLAN TRANSLATION #####################")
    _LOGGER.debug("VLAN mapping %s on %s", vlan_map, on_bridge)

    # Split VLAN map
    source_vlan, dest_vlan = vlan_map.split(":")
    vlan_hash = _hash_string(vlan_map)

    # Check if veth pair already exists
    veth0 = f"v0_{vlan_hash}"
    veth1 = f"v1_{vlan_hash}"
    _LOGGER.debug("VETH pair entry: %s <--> %s", veth0, veth1)

    # We will always check the C-VLAN veth endpoint.
    if not _veth_exists(veth0):
        # Create veth pair
        _run_command(f"ip link add {veth0} type veth peer name {veth1}")
        _run_command(f"ip link set dev {veth0} up")
        _run_command(f"ip link set dev {veth1} up")

        _LOGGER.info("VETH pair created: %s <--> %s", veth0, veth1)
    else:
        _LOGGER.debug(
            "VETH pair %s <--> %s  exists on the host!!",
            veth0,
            veth1,
        )
        _LOGGER.debug("Skipping VLAN endpoint creation.")

    # Loop through the bridges and attach veth ends, set VLAN tags, and external_ids
    for veth_name, vlan in [
        (veth0, source_vlan),
        (veth1, dest_vlan),
    ]:
        # Check if the interface already exists
        check = _run_command(f"ovs-vsctl port-to-br {veth_name}", check=False)

        if check.returncode == 1:
            # OVS Port does not exist, create one
            _LOGGER.debug("VETH %s is not part of any OVS bridge!!", veth_name)
            _run_command(f"ovs-vsctl --may-exist add-port {on_bridge} {veth_name}")
            _LOGGER.info("VETH %s attached to bridge %s", veth_name, on_bridge)

            # Set VLAN tag on the bridge interface
            _run_command(f"ovs-vsctl set port {veth_name} tag={vlan}")
            _LOGGER.info("VLAN tag %s set on port %s", vlan, veth_name)
            continue

        if check.stdout.strip() != on_bridge:
            # The config.json is messy!
            # Alert the user!
            msg = f"{veth_name} already a member of {check.stdout.strip()}"
            raise ValueError(msg)


def add_iface_to_container(  # noqa: PLR0915, C901, PLR0912
    container_name: str,
    info: ContainerInfo,
) -> None:
    """Attach a container to a target OVS bridge.

    :param container_name: Target container to push the interface at
    :type container_name: str
    :param info: Container interface details
    :type info: ContainerInfo
    :raises ValueError: If ipaddress syntax is incorrect.
    :raises IndexError: If DHCP range is exhausted.
    """
    _LOGGER.debug("###################ADD IFACE TO CONTAINERS######################")

    # Check if container exists, skip if it does not exist.
    check = _run_command(f"docker ps -f name={container_name}$ -q", check=False)
    if not bool(check.stdout.strip()):
        _LOGGER.debug("Container %s does not exist!", container_name)
        return

    _LOGGER.debug("Container ID: %s", check.stdout.strip())

    util = "ovs-docker" if not _USE_LINUX_BRIDGE else "lxbr-docker"

    bridge = info["bridge"]  # Mandatory
    db_cache = _get_db()[bridge]

    iface = info["iface"]  # Mandatory
    vlan = info.get("vlan")
    trunk = info.get("trunk")

    cmd = f"{util} add-port {bridge} {iface} {container_name}"
    for ip_idx, gw_idx in (("ipaddress", "gateway"), ("ip6address", "gateway6")):
        family = (
            ipaddress.IPv4Interface
            if ip_idx == "ipaddress"
            else ipaddress.IPv6Interface
        )

        if gateway := info.get(gw_idx):
            cmd = f"{cmd} --{gw_idx}={gateway}"

        hosts = db_cache.get(
            "iprange_hosts" if ip_idx == "ipaddress" else "ip6range_hosts"
        )

        if ipaddr := info.get(ip_idx):
            # If the user explicitly specifies "No-IP" in ipaddress
            # Then skip
            if ipaddr == "No-IP":
                continue

            if "/" not in str(ipaddr):
                msg_no_prefix = f"{container_name}: ip {ipaddr} must have a prefix mask"
                raise ValueError(msg_no_prefix)
            if not isinstance(ipaddress.ip_interface(str(ipaddr)), family):
                msg = (
                    f"{container_name}: {ip_idx} {ipaddr} "
                    f"must be valid {family.__name__} address"
                )
                raise ValueError(msg)

            if ipaddr != hosts.get(container_name):
                hosts.pop(container_name, None)
                if ipaddr in hosts.values():
                    msg_ip_exists = (
                        f"IP {ipaddr} already allocated to someone else.",
                        f"Failed to assign addr to container: {container_name}",
                    )
                    raise ValueError(msg_ip_exists)

            hosts[container_name] = ipaddr
            cmd = f"{cmd} --{ip_idx}={ipaddr}"

        # If ipaddress is not provided, but the bridge has an iprange defined
        # The container will be provided with an IP address from  bridge's
        # ip range.
        elif ip_range := db_cache.get(
            "iprange" if ip_idx == "ipaddress" else "ip6range"
        ):
            network_hosts = ipaddress.ip_network(str(ip_range)).hosts()

            # Skip first 5 host values
            _ = [next(network_hosts) for _ in range(5)]

            for host in network_hosts:
                ipaddr = f"{host}/{ip_range.split('/')[-1]}"
                if ipaddr not in hosts.values():
                    _LOGGER.debug(
                        "Automatic IP allocation (%s) to container: %s",
                        ipaddr,
                        container_name,
                    )
                    hosts[container_name] = ipaddr
                    cmd = f"{cmd} --{ip_idx}={ipaddr}"
                    break
            else:
                _LOGGER.debug("Subnet exhausted %s", ip_range)
                msg_subnet_exhausted = (
                    "Failed to automatically allocate an",
                    f" IP to container: {container_name}",
                )
                raise IndexError(msg_subnet_exhausted)

    if macaddress := info.get("macaddress", ""):
        cmd = f"{cmd} --macaddress={macaddress}"

    # check if iface exists already inside the container
    check = _run_command(
        f"docker exec {container_name} ip link show {iface}", check=False
    )
    if check.returncode == 0:
        _LOGGER.debug("iface %s exists inside container %s.", iface, container_name)
        check = _run_command(f"{util} get-port {container_name} {iface}", check=False)
        if bool(check.stdout.strip()):
            _LOGGER.debug(
                "iface %s exists on OVS bridge: %s.",
                iface,
                check.stdout.strip(),
            )
            check = _run_command(
                f"docker exec {container_name} ip link show dev {iface}", check=False
            )
            if macaddress.lower() not in check.stdout:
                _run_command(
                    f"docker exec {container_name} ip link set dev {iface} addr {macaddress}",
                    check=False,
                )
            return

        # Found a corner case
        # If OVS orchestrator service is restarted,
        # previously deployed containers still have their old interfaces.
        # However, ovs does not have a link to them.
        _LOGGER.debug(
            "iface %s exists inside container %s, but not on OVS",
            iface,
            container_name,
        )

        # In that case we need to clean the container interface ourself.
        # NOTE: Add a way to not reload OVS kernel modules everytime to avoid this.
        _run_command(f"docker exec {container_name} ip link del {iface}", check=False)
        _run_command(f"{util} del-port {bridge} {iface} {container_name}", check=False)

        _LOGGER.info("Removed iface %s from container: %s", iface, container_name)
    else:
        _LOGGER.info("Container: %s is missing interface %s!!", container_name, iface)

        # del the OVS mapped port
        # If container has lost its interface
        _run_command(f"{util} del-port {bridge} {iface} {container_name}", check=False)

    _run_command(cmd)
    _LOGGER.info(
        "Interface %s connected to bridge:%s added to container %s",
        iface,
        bridge,
        container_name,
    )
    if not vlan and not trunk:
        return

    if vlan:
        _LOGGER.debug("VLAN tag read for %s:%s is %s", container_name, iface, vlan)
        run(
            f"{util} set-vlan {bridge} {iface} {container_name} " f"{vlan}".split(),
            check=True,
            capture_output=True,
        )
        _LOGGER.info("VLAN tag set for %s:%s is %s", container_name, iface, vlan)

    if trunk:
        _LOGGER.debug(
            "Trunk (%s) read for %s:%s on %s", trunk, container_name, iface, bridge
        )
        run(
            f"{util} set-trunk {bridge} {iface} {container_name} " f"{trunk}".split(),
            check=True,
            capture_output=True,
        )
        _LOGGER.info(
            "Trunk (%s) set for %s:%s on %s", trunk, container_name, iface, bridge
        )


def _check_ovs_module() -> None:
    # Initial check if openvswitch module is loaded.
    lsmod_out = run(["/bin/lsmod"], check=True, capture_output=True)
    try:
        run(
            ["grep", "openvswitch"],
            check=True,
            input=lsmod_out.stdout,
            capture_output=True,
        )
    except CalledProcessError:
        _LOGGER.exception("Openvswitch kernel modules need to be mounted from host!!")
        sys.exit(1)


def main() -> None:  # noqa: C901, PLR0912
    """Runner function."""
    # Initial Check if docker socket is loaded.
    if not _DOCKER_SOCKET.exists():
        _LOGGER.error("Need to mount Docker socket!!")
        sys.exit(1)

    if not _USE_LINUX_BRIDGE:
        _check_ovs_module()
    else:
        _run_command("sysctl net.bridge.bridge-nf-call-iptables=0")

    config = json.load(Path("/root/config.json").open(encoding="UTF-8"))  # noqa: SIM115

    fail_count = _get_db().setdefault("failed", 0)
    if fail_count > _MAX_FAIL_COUNT:
        sys.exit(1)

    try:
        # Initialize all parent bridges
        for bridge, info in config["bridge"].items():
            init_bridge(bridge, info)

        # Attach containers to parent bridges based on config.json
        for container, iface_info in config["container"].items():
            for info in iface_info:
                add_iface_to_container(container, info)

        # Configure VLAN translations between bridges
        # Skipping VLAN translations for default linux bridges!
        if not _USE_LINUX_BRIDGE:
            for translation in config["vlan_translations"]:
                create_veth_pair(
                    on_bridge=translation["on"],
                    vlan_map=translation["map"],
                )

        # Introduce a sleep since supervisor can't add interval between restarts.
        time.sleep(10)

    except (CalledProcessError, ValueError, IndexError):
        _LOGGER.exception("Exiting core due to exception")
        traceback.print_exc()
        _get_db()["failed"] = fail_count + 1
        if fail_count > _MAX_FAIL_COUNT:
            _LOGGER.exception("Orchestrator keeps failing!")
            _LOGGER.exception("Bailing out!! Will not attempt to modify host network!!")

    finally:
        # Dump the Lease DB
        json.dump(
            _get_db(),
            _DB_JSON_PATH.open("w", encoding="utf-8"),
            indent=4,
        )


if __name__ == "__main__":
    main()
