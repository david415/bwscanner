
from twisted.trial import unittest
from twisted.internet import defer, task
from twisted.test import proto_helpers

from txtorcon import TorControlProtocol
from txtorcon.router import hashFromHexId

from bwscanner.detect_partitions import ProbeAll2HopCircuits
from bwscanner.circuit import FullyConnected, to_pair


class FakeTorState(object):

    def __init__(self, routers):
        self.routers = routers
        self.protocol = TorControlProtocol()
        self.protocol.connectionMade = lambda: None
        self.protocol.transport = proto_helpers.StringTransport()
        self.protocol.makeConnection(self.protocol.transport)

    def _find_circuit_after_extend(self, x):
        return defer.succeed(None)

    def build_circuit(self, routers=None, using_guards=True):
        cmd = "EXTENDCIRCUIT 0 "
        first = True
        for router in routers:
            if first:
                first = False
            else:
                cmd += ','
            if isinstance(router, basestring) and len(router) == 40 \
               and hashFromHexId(router):
                cmd += router
            else:
                cmd += router.id_hex[1:]
        d = self.protocol.queue_command(cmd)
        d.addCallback(self._find_circuit_after_extend)
        return d


class FakeRouter:

    def __init__(self, i):
        self.id_hex = i
        self.flags = []


class ProbeTests(unittest.TestCase):

    def test_basic(self):
        routers = {}
        for x in range(3):
            routers.update({"router%r" % (x,): FakeRouter("$%040d" % x)})

        tor_state = FakeTorState(routers)
        tor_state._attacher_error = lambda f: f
        tor_state.transport = proto_helpers.StringTransport()

        clock = task.Clock()
        log_dir = "."
        stop_hook = defer.Deferred()

        def stopped():
            stop_hook.callback(None)
        probe = ProbeAll2HopCircuits(tor_state, clock, log_dir, stopped)
        probe.run_scan()
        clock.advance(10)
        clock.advance(10)
        clock.advance(10)
        clock.advance(10)
        clock.advance(10)
        clock.advance(10)
        clock.advance(10)
        clock.advance(10)
        clock.advance(10)
        clock.advance(10)
        return stop_hook


class PermutationsTests(unittest.TestCase):

    def test_to_pair(self):
        n = 5
        print ""
        print to_pair(3, 4)
        print ""

        pairs = [to_pair(x, n) for x in range(n*(n-1)/2)]
        print ""
        print pairs
        print ""
        self.failUnlessEqual(pairs, [(0, 1), (0, 2), (0, 3), (0, 4), (1, 2),
                                     (1, 3), (1, 4), (2, 3), (2, 4), (3, 4)])

    def test_permutations(self):
        class FakeTorState(object):
            def __init__(self, routers):
                self.routers = routers
        routers = {
            "relay1": FakeRouter(123),
            "relay2": FakeRouter(234),
            "relay3": FakeRouter(345),
            "relay4": FakeRouter(456),
        }
        tor_state = FakeTorState(routers)
        circuit_generator = FullyConnected(tor_state)

        # Use a set for the path so the order does not affect the comparison
        results = [set(circuit) for circuit in circuit_generator]

        # XXX: Each pairing will only be produced once, the reverse
        #      pairing will not be created. Is that the correct behaviour?
        self.failUnlessEqual(len(results), 6)
        self.failUnless({routers["relay1"], routers["relay2"]} in results)
        self.failUnless({routers["relay1"], routers["relay3"]} in results)
        self.failUnless({routers["relay1"], routers["relay4"]} in results)
        self.failUnless({routers["relay2"], routers["relay3"]} in results)
        self.failUnless({routers["relay2"], routers["relay4"]} in results)
        self.failUnless({routers["relay3"], routers["relay4"]} in results)

    def test_display_permutations(self):
        class FakeTorState(object):
            def __init__(self, routers):
                self.routers = routers
        routers = {}
        for i in range(7000):
            routers["relay%s" % i] = FakeRouter(i)

        tor_state = FakeTorState(routers)
        circuit_generator = FullyConnected(tor_state, this_partition=0, partitions=10)

        print ""
        for circuit in circuit_generator:
            hop1 = circuit[0]
            hop2 = circuit[1]
            print "%s -> %s" % (hop1.id_hex, hop2.id_hex)

