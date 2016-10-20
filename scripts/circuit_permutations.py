#!/usr/bin/env python

import click
import hashlib

from stem.descriptor import parse_file

from bwscanner.partition_shuffle import lazy2HopCircuitGenerator


def get_relay_list_from_consensus(consensus):
    relays = []
    with open(consensus, 'rb') as consensus_file:
        for relay in parse_file(consensus_file):
            if relay is not None and relay.fingerprint is not None:
                relays.append(relay.fingerprint)
    return relays

def get_relay_list_from_file(relay_list_file):
    relays = []
    with open(relay_list_file, "r") as rf:
        relay_lines = rf.read()
    for relay_line in relay_lines.split():
        relays.append(relay_line)
    return relays


@click.command()
@click.option('--relay-list', default=None, type=str, help="file containing list of tor relay fingerprints, one per line")
@click.option('--consensus', default=None, type=str, help="file containing tor consensus document, network-status-consensus-3 1.0")
@click.option('--secret', default=None, type=str, help="secret")
@click.option('--partitions', default=None, type=int, help="total number of permuation partitions")
@click.option('--this-partition', default=None, type=int, help="which partition to scan")
def main(relay_list, consensus, secret, partitions, this_partition):

    if consensus is not None:
        relays = get_relay_list_from_consensus(consensus)
    elif relay_list is not None:
        relays = get_relay_list_from_file(relay_list)
    else:
        pass  # XXX todo: print usage

    consensus_str = ""
    for relay in relays:
        consensus_str += relay + ","
    consensus_hash = hashlib.sha256(consensus_str).digest()
    shared_secret_hash = hashlib.sha256(secret).digest()
    prng_seed = hashlib.pbkdf2_hmac('sha256', consensus_hash, shared_secret_hash, iterations=1)

    circuits = lazy2HopCircuitGenerator(relays, this_partition, partitions, prng_seed)

    for circuit in circuits:
        print "%s,%s" % (circuit[0], circuit[1])
    
if __name__ == '__main__':
    main()
