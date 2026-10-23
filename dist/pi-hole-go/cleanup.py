#!/usr/bin/env python3
"""Also invoked by systemd after crashes: remove only Pi-hole Go firewall hooks."""
from filtering.network import Network

if __name__ == '__main__':
    Network().cleanup()
