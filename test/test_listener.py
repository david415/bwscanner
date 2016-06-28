from twisted.internet.protocol import Protocol, Factory
from twisted.internet import defer, reactor
from twisted.web.client import readBody
from twisted.web.resource import Resource
from twisted.web.server import Site
from twisted.trial.unittest import SkipTest
from twisted.python import log
from twisted.internet import task
from twisted.internet.endpoints import clientFromString, connectProtocol, serverFromString
from twisted.protocols.policies import WrappingFactory, ProtocolWrapper

from twisted.trial import unittest
from collections import deque
from txtorcon.circuit import Circuit
from txtorcon.util import available_tcp_port

from bwscanner.listener import CircuitEventListener, StreamBandwidthListener
from bwscanner.fetcher import OnionRoutedAgent
from test.template import TorTestCase

import random
import time


class NotEnoughMeasurements(SkipTest):
    pass

class DelayDeque(object):
    def __init__(self, maxlen, clock, handle_data):
        print "delay deque init"
        self.maxlen = maxlen
        self.clock = clock
        self.handle_data = handle_data

        self.turn_delay = 0
        self.deque = deque(maxlen=self.maxlen)
        self.is_ready = False
        self.stopped = False
        self.lazy_tail = defer.succeed(None)
        self.clear_deque_timer = None
        self.clear_deque_duration = 10
        self.fire_when_empty = defer.Deferred()

    def ready(self):
        print "delay deque ready"
        self.is_ready = True
        self.stopped = False
        self.turn_deque()

    def pause(self):
        print "delay deque pause"
        self.is_ready = False
        self.stopped = True

    def stop_all_timers(self):
        print "delay deque stop all timers"
        if self.clear_deque_timer.active():
            self.clear_deque_timer.cancel()

    def append(self, data):
        print "delay deque append"
        if self.clear_deque_timer is None:
            self.clear_deque_timer = self.clock.callLater(self.clear_deque_duration, self.deque.clear)
        else:
            if self.clear_deque_timer.active():
                self.clear_deque_timer.reset(self.clear_deque_duration)
            else:
                self.clear_deque_timer = self.clock.callLater(self.clear_deque_duration, self.deque.clear)

        self.deque.append(data)
        if self.is_ready:
            self.clock.callLater(0, self.turn_deque)

    def turn_deque(self):
        print "delay deque turn deque"
        if self.stopped:
            return
        try:
            data = self.deque.pop()
        except IndexError:
            self.lazy_tail.addCallback(lambda ign: defer.succeed(None))
            self.fire_when_empty.callback(None)
        else:
            self.lazy_tail.addCallback(lambda ign: self.handle_data(data))
            self.lazy_tail.addErrback(log.err)
            self.lazy_tail.addCallback(lambda ign: task.deferLater(self.clock, self.turn_delay, self.turn_deque))

class TrickleProtocol(ProtocolWrapper):
    """
    Protocol for L{TrickelFactory}.
    """

    def write(self, data):
        print "trickle protocol write"
        ProtocolWrapper.dataReceived(self, data)
        #for b in data:
        #    self.send_deque.append(b)

    def writeSequence(self, seq):
        print "trickle protocol write sequence"
        ProtocolWrapper.writeSequence(self, seq)
        #for data in seq:
        #    self.write(data)

    def dataReceived(self, data):
        print "trickle protocol data received"
        ProtocolWrapper.dataReceived(self, data)
        #for b in data:
        #    self.receive_deque.append(b)

    def handleReceive(self, data):
        print "trickel protocol handle received data"
        ProtocolWrapper.dataReceived(self, data)

    def handleSend(self, data):
        print "tricket protocol handle send data"
        ProtocolWrapper.write(self, data)

    def registerProducer(self, producer, streaming):
        print "trickle protocl register producer"
        self.producer = producer
        ProtocolWrapper.registerProducer(self, producer, streaming)

    def unregisterProducer(self):
        print "trickle protocol unregister producer"
        del self.producer
        ProtocolWrapper.unregisterProducer(self)

class TrickleFactory(WrappingFactory):
    """
    Sends data in a slow trickle.
    """

    protocol = TrickleProtocol

    def __init__(self, wrappedFactory):
        print "trickle factory init"
        WrappingFactory.__init__(self, wrappedFactory)
        self.my_protocol = None

    def buildProtocol(self, addr):
        print "trickle factory build protocol"
        self.my_protocol = WrappingFactory.buildProtocol(self, addr)
        # XXX
        #self.my_protocol.receive_deque = DelayDeque(100, reactor, self.my_protocol.handleReceive)
        #self.my_protocol.receive_deque.ready()
        #self.my_protocol.send_deque = DelayDeque(100, reactor, self.my_protocol.handleSend)
        #self.my_protocol.send_deque.ready()
        return self.my_protocol

    def unregisterProtocol(self, p):
        print "trickle factory unregister protocol"
        WrappingFactory.unregisterProtocol(self, p)

class DummyClientProtocol(Protocol):
    def connectionMade(self):
        self.transport.write("hello\r\n")
    def dataReceived(self, data):
        print "client received data: %s" % (data,)
        #self.transport.loseConnection()

class DummyClientFactory(Factory):
    def buildProtocol(self, addr):
        self.protocol = DummyClientProtocol()
        return self.protocol

class DummyServerFactory(Factory):
    def buildProtocol(self, addr):
        self.protocol = DummyServerProtocol()
        return self.protocol

class DummyServerProtocol(Protocol):
    def connectionMade(self):
        self.transport.write("hiya\r\n")
    def dataReceived(self, data):
        print "server received data: %s" % (data,)


class TestTrickle(unittest.TestCase):
    def test_blah(self):
        def print_protocol(result):
            print "print_protocol ---"
            print result
            return result
        def print_failure(f):
            print f
            return f

        #factory = TrickleFactory(server)
        server_endpoint = serverFromString(reactor, "tcp:interface=127.0.0.1:8080")
        server_factory = DummyServerFactory()
        d = server_endpoint.listen(server_factory)
        d.addCallback(print_protocol)
        d.addErrback(print_failure)

        client_endpoint = clientFromString(reactor, "tcp:127.0.0.1:8080")
        client_factory = DummyClientFactory()
        print "before connectProtocol"
        d2 = client_endpoint.connect(client_factory)
        d2.addCallback(print_protocol)
        d2.addErrback(print_failure)

        end_d = defer.DeferredList([d,d2])
        def cleanup():
            print "---- CLEANUP cleanup"
            d.cancel()
            client_factory.protocol.transport.loseConnection()
            server_factory.protocol.transport.loseConnection()
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

    @defer.inlineCallbacks
    def setUp(self):
        yield super(TestStreamBandwidthListener, self).setUp()
        #self.fetch_size = 8*2**20  # 8MB
        self.fetch_size = 100  # 8MB
        fetch_size = self.fetch_size
        self.stream_bandwidth_listener = yield StreamBandwidthListener(self.tor)

        class DummyResource(Resource):
            isLeaf = True

            def render_GET(self, request):
                return 'a' * fetch_size

        self.port = yield available_tcp_port(reactor)
        self.site = Site(DummyResource())
        self.factory = TrickleFactory(self.site)
        self.test_service = reactor.listenTCP(self.port, self.factory)

        self.not_enough_measurements = NotEnoughMeasurements(
            "Not enough measurements to calculate STREAM_BW samples.")

    @defer.inlineCallbacks
    def test_circ_bw(self):
        print "before fetch"
        r = yield self.do_fetch()
        print "before bw_events"
        bw_events = self.stream_bandwidth_listener.circ_bw_events.get(r['circ'])
      
        #def yo(result):
        #    print "YOIYOIYOYOYOYOOY"
        #self.factory.my_protocol.receive_d.addCallback(yo)
        assert bw_events
        print bw_events
        # XXX: why are the counters reversed!? -> See StreamBandwidthListener
        #      docstring.
        # assert self.fetch_size/2 <= sum([x[1] for x in bw_events]) <= self.fetch_size
        #assert sum([x[1] for x in bw_events]) <= self.fetch_size
        # either this is backward, or we wrote more bytes than read?!
        #assert sum([x[2] for x in bw_events]) >= sum([x[1] for x in bw_events])

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
        body = yield readBody(request)
        assert len(body) == self.fetch_size
        circ = [c for c in self.tor.circuits.values() if c.path == path][0]
        assert isinstance(circ, Circuit)

        # XXX: Wait for circuit to close, then I think we can be sure that
        #      the BW events have been emitted.
        yield circ.close(ifUnused=True)
        defer.returnValue({'duration': time.time() - time_start, 'circ': circ})

    @defer.inlineCallbacks
    def tearDown(self):
        yield super(TestStreamBandwidthListener, self).tearDown()
        yield self.test_service.stopListening()
