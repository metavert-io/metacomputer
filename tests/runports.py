"""Port numbers of this test run's own, for the no-VM suites' stand-in VMs.

metacomputer tells one VM's connections from another's by the VM's loopback SSH port (lib/owners.py for_this_vm: an
`ssh ... -p PORT ...@127.0.0.1` is this VM's), and the hub, the input reset and the session guard look at every such
process on the Mac. A suite that gave its stand-in VM a fixed number (1, 59995, ...) saw the stand-in connections of
any other run using the same number at the same time: another copy of the suite (a second checkout's), or another
suite. port() asks the system for a free loopback port and keeps it bound (never listening, so nothing can connect to
it) until this process ends: no two runs on one Mac get the same number while both run.

    import runports
    SSH_PORT = runports.port()
"""
import socket

_held = []


def port():
    """A loopback port number no other running process holds, held by this one until it ends."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    _held.append(s)
    return s.getsockname()[1]
