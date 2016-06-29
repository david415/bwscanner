from twisted.internet.protocol import Protocol, Factory
from twisted.internet import defer, reactor
from twisted.web.client import readBody
from twisted.web.resource import Resource
from twisted.web.server import Site
from twisted.trial.unittest import SkipTest
from twisted.internet import task
from twisted.internet.endpoints import clientFromString, serverFromString
from twisted.protocols.policies import ProtocolWrapper
from twisted.trial import unittest

from txtorcon.circuit import Circuit
from txtorcon.util import available_tcp_port

from bwscanner.listener import CircuitEventListener, StreamBandwidthListener
from bwscanner.fetcher import OnionRoutedAgent
from test.template import TorTestCase

import random
import time


class NotEnoughMeasurements(SkipTest):
    pass

class DummyClientProtocol(Protocol):
    def connectionMade(self):
        self.transport.write("hello")
    def dataReceived(self, data):
        print "\nclient received data: %s" % (data,)
        #self.transport.loseConnection()
    def connectionLost(self, reason):
        self.connection_lost_d.callback(None)

class DummyClientFactory(Factory):
    def buildProtocol(self, addr):
        self.protocol = DummyClientProtocol()
        self.protocol.connection_lost_d = defer.Deferred()
        return self.protocol

class TricklingBufferingProtocol(ProtocolWrapper):

    def __init__(self, factory, wrappedProtocol):
        ProtocolWrapper.__init__(self, factory, wrappedProtocol)
        self.buffer = []
        self.connection_lost_d = defer.Deferred()

    def delayedWrite(self, data, offset=0, size=1, delay=.1):
        if offset >= len(data):
            return
        if (offset + size) > len(data) -1:
            b = data[offset:]
            self.transport.write(b)
            return
        else:
            # XXX
            # maybe replace with twisted.python.compat.lazyByteSlice if you seek py3 compatibility
            b = data[offset:offset+size]
        self.transport.write(b)
        reactor.callLater(delay, self.delayedWrite, data, offset+1, size, delay)

    def write(self, data):
        self.delayedWrite(data)

    def writeSequence(self, seq):
        for s in seq:
            self.write(s)

    def dataReceived(self, data):
        ProtocolWrapper.dataReceived(self, data)

    def connectionLost(self, reason):
        self.connection_lost_d.callback(None)

    def registerProducer(self, producer, streaming):
        self.producer = producer
        ProtocolWrapper.registerProducer(self, producer, streaming)

    def unregisterProducer(self):
        del self.producer
        ProtocolWrapper.unregisterProducer(self)


class TricklingWrappingFactory(Factory):

    def __init__(self, wrapped_factory=None, wrapped_protocol=None):
        self.protocols = {}
        self.wrapped_factory = wrapped_factory
        self.wrapped_protocol = wrapped_protocol
        self.my_protocol = None

    def buildProtocol(self, addr):
        if self.wrapped_factory is not None and self.wrapped_protocol is None:
            inner_protocol = self.wrapped_factory.buildProtocol(addr)
            self.my_protocol = TricklingBufferingProtocol(self, inner_protocol)
        else:
            inner_protocol = self.wrapped_protocol
            self.my_protocol = TricklingBufferingProtocol(self, inner_protocol)

        return self.my_protocol

    def registerProtocol(self, p):
        """
        Called by protocol to register itself.
        """
        self.protocols[p] = 1

    def unregisterProtocol(self, p):
        """
        Called by protocols when they go away.
        """
        del self.protocols[p]

class DummyServerProtocol(Protocol):
    def connectionMade(self):
        self.transport.write("hiya!")
    def dataReceived(self, data):
        print "\nserver received data: %s" % (data,)


class TestTrickle(unittest.TestCase):
    def test_blah(self):
        server_endpoint = serverFromString(reactor, "tcp:interface=127.0.0.1:8080")
        server_factory = TricklingWrappingFactory(wrapped_protocol=DummyServerProtocol())
        d = server_endpoint.listen(server_factory)

        client_endpoint = clientFromString(reactor, "tcp:127.0.0.1:8080")
        client_factory = DummyClientFactory()
        d2 = client_endpoint.connect(client_factory)

        def fail_connect(f):
            print "FAILURE %s" % (f,)
            return f
        d2.addErrback(fail_connect)

        def stopListening(listeningPort):
            return listeningPort.stopListening()
        d.addCallback(lambda listeningPort: task.deferLater(reactor, 3, stopListening, listeningPort))

        end_d = defer.DeferredList([d,d2])
        def cleanup():
            client_factory.protocol.transport.loseConnection()
            lost_d = defer.DeferredList([client_factory.protocol.connection_lost_d, server_factory.my_protocol.connection_lost_d])
            return lost_d
        end_d.addCallback(lambda ign: task.deferLater(reactor, 3, cleanup))
        return end_d

    def test_http(self):
        fetch_size = 100
        class DummyResource(Resource):
            isLeaf = True
            def render_GET(self, request):
                return 'a' * fetch_size

        server_endpoint = serverFromString(reactor, "tcp:interface=127.0.0.1:8080")
        site_factory = Site(DummyResource())
        server_factory = TricklingWrappingFactory(wrapped_factory=site_factory)
        d = server_endpoint.listen(server_factory)

        client_endpoint = clientFromString(reactor, "tcp:127.0.0.1:8080")
        client_factory = DummyClientFactory()
        d2 = client_endpoint.connect(client_factory)

        def stopListening(listeningPort):
            return listeningPort.stopListening()
        d.addCallback(lambda listeningPort: task.deferLater(reactor, 3, stopListening, listeningPort))

        end_d = defer.DeferredList([d,d2])
        def cleanup():
            client_factory.protocol.transport.loseConnection()
            #lost_d = defer.DeferredList([client_factory.protocol.connection_lost_d, server_factory.protocol.connection_lost_d])
            #return lost_d
        end_d.addCallback(lambda ign: task.deferLater(reactor, 3, cleanup))
        return end_d

class FakeCircuit(Circuit):
    def __init__(self, id=None, state='BOGUS'):
        self.streams = []
        self.purpose = ''
        self.path = []
        self.id = id or random.randint(2222, 7777)
        self.state = state


class TestCircuitEventListener(TorTestCase):
    @defer.inlineCallbacks
    def setUp(self):
        yield super(TestCircuitEventListener, self).setUp()

        # XXX - this looks sloppy that we pass self.tor to the
        # CircuitEventListener as well as pass that reference
        # to a method call on self.tor; maybe like we could
        # do with one less reference to self.tor
        self.circuit_event_listener = CircuitEventListener(self.tor)
        self.tor.add_circuit_listener(self.circuit_event_listener)

    @defer.inlineCallbacks
    def test_circuit_lifecycle(self):
        path = self.random_path()
        circ = yield self.attacher.create_circuit('127.0.0.1', 1234, path)
        self.assertIsInstance(circ, Circuit)
        self.assertEqual(circ.path, path)
        circuit_lifecycle = self.circuit_event_listener.circuits[circ]
        # XXX argh, we haven't gotten all the events from Tor yet...
        # hax to block until we've made Tor do something...
        yield circ.close(ifUnused=False)
        yield self.tor.protocol.get_info('version')
        expected_states = ['circuit_new', 'circuit_launched', 'circuit_extend',
                           'circuit_extend', 'circuit_extend', 'circuit_built',
                           'circuit_closed']
        assert len(circuit_lifecycle) == len(expected_states)
        assert [k['event'] for k in circuit_lifecycle] == expected_states

            
class TestStreamBandwidthListener(TorTestCase):
    #skip = "broken tests"

    def setUp(self):
        #self.fetch_size = 8*2**20  # 8MB
        self.fetch_size = 12
        fetch_size = self.fetch_size


        class DummyResource(Resource):
            isLeaf = True

            def render_GET(self, request):
                return 'a' * fetch_size

        d = super(TestStreamBandwidthListener, self).setUp()
        def get_bandwidth_listener(result):
            return StreamBandwidthListener(self.tor)
        d.addCallback(get_bandwidth_listener)
        def set_bandwidth_listener(result):
            self.stream_bandwidth_listener = result
        d.addCallback(set_bandwidth_listener)
        def get_port(result):
            return available_tcp_port(reactor)
        d.addCallback(get_port)
        def set_port(port):
            self.port = port
        d.addCallback(set_port)
        def setup_site(result):
            self.site = Site(DummyResource())
            self.factory = TricklingWrappingFactory(wrapped_factory=self.site)
            server_endpoint = serverFromString(reactor, "tcp:interface=127.0.0.1:%s" % self.port)
            return server_endpoint.listen(self.factory)
        d.addCallback(setup_site)
        def get_port_listener(result):
            self.test_service = result
        d.addCallback(get_port_listener)
        def err(f):
            print "failed to setup an http listener: %s" % (f,)
            return f
        d.addErrback(err)
        return d

    def test_circ_bw(self):
        print "before fetch"
        d = self.do_fetch()
        def test_results(result):
            print "test_results"
            bw_events = self.stream_bandwidth_listener.circ_bw_events.get(result['circ'])
            assert bw_events
            print "TEST RESULTS: %s" % (bw_events,)
        d.addCallback(lambda ign: task.deferLater(reactor, 5, test_results))

        # XXX: why are the counters reversed!? -> See StreamBandwidthListener
        #      docstring.
        # assert self.fetch_size/2 <= sum([x[1] for x in bw_events]) <= self.fetch_size
        #assert sum([x[1] for x in bw_events]) <= self.fetch_size
        # either this is backward, or we wrote more bytes than read?!
        #assert sum([x[2] for x in bw_events]) >= sum([x[1] for x in bw_events])
        self.current_task_d = d
        return d

    @defer.inlineCallbacks
    def test_stream_bw(self):
        r = yield self.do_fetch()
        bw_events = self.stream_bandwidth_listener.stream_bw_events.get(r['circ'])
        assert bw_events
        assert self.fetch_size/2 <= sum([x[1] for x in bw_events]) <= self.fetch_size

    @defer.inlineCallbacks
    def test_bw_samples(self):
        r = yield self.do_fetch()
        bw_events = self.stream_bandwidth_listener.stream_bw_events.get(r['circ'])
        assert bw_events
        # XXX: Where are these self.fetch_size/n magic values coming from?
        assert self.fetch_size/4 <= sum([x[1] for x in bw_events]) <= self.fetch_size

        # XXX: If the measurement happens in under 1 second, we will have one
        #      STREAM_BW, and will not be able to calculate BW samples.
        if len(bw_events) == 1:
            raise self.not_enough_measurements
        bw_samples = [x for x in self.stream_bandwidth_listener.bw_samples(r['circ'])]
        assert bw_samples
        assert self.fetch_size/2 <= sum([x[0] for x in bw_samples]) <= self.fetch_size
        assert r['duration'] * .5 < sum([x[2] for x in bw_samples]) < r['duration'] * 2

    @defer.inlineCallbacks
    def test_circ_avg_bw(self):
        r = yield self.do_fetch()
        bw_events = self.stream_bandwidth_listener.stream_bw_events.get(r['circ'])
        # XXX: these complete too quickly to sample sufficient bytes...
        assert bw_events
        assert self.fetch_size/4 <= sum([x[1] for x in bw_events]) <= self.fetch_size

        if len(bw_events) == 1:
            raise self.not_enough_measurements
        circ_avg_bw = self.stream_bandwidth_listener.circ_avg_bw(r['circ'])
        assert circ_avg_bw is not None
        assert circ_avg_bw['path'] == r['circ'].path
        assert self.fetch_size/4 <= circ_avg_bw['bytes_r'] <= self.fetch_size
        assert 0 < circ_avg_bw['duration'] <= r['duration']
        assert (circ_avg_bw['bytes_r']/4 < (circ_avg_bw['samples'] * circ_avg_bw['r_bw']) <
                circ_avg_bw['bytes_r']*2)

    @defer.inlineCallbacks
    def do_fetch(self):
        time_start = time.time()
        path = self.random_path()
        agent = OnionRoutedAgent(reactor, path=path, state=self.tor)
        url = "http://127.0.0.1:{}".format(self.port)
        request = yield agent.request("GET", url)
        print request
        body = yield readBody(request)
        print body
        assert len(body) == self.fetch_size
        circ = [c for c in self.tor.circuits.values() if c.path == path][0]
        assert isinstance(circ, Circuit)

        # XXX: Wait for circuit to close, then I think we can be sure that
        #      the BW events have been emitted.
        yield circ.close(ifUnused=True)
        defer.returnValue({'duration': time.time() - time_start, 'circ': circ})

    def tearDown(self):
        #d = self.current_task_d
        d = defer.succeed(None)
        d.addCallback(lambda ign: super(TestStreamBandwidthListener, self).tearDown())
        d.addCallback(lambda ign: self.test_service.stopListening())
        return d

