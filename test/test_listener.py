from twisted.internet import defer, reactor
from twisted.internet.endpoints import serverFromString
from twisted.web.client import readBody
from twisted.web.resource import Resource
from twisted.web.server import Site
from twisted.trial.unittest import SkipTest

from twisted.protocols.policies import ThrottlingFactory

from txtorcon.circuit import Circuit
from txtorcon.util import available_tcp_port

from bwscanner.listener import CircuitEventListener, StreamBandwidthListener
from bwscanner.fetcher import OnionRoutedAgent
from test.template import TorTestCase

import random
import time


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

    def setUp(self):
        d = super(TestStreamBandwidthListener, self).setUp()
        self.fetch_size = 8*2**20  # 8MB
        d.addCallback(lambda ign: StreamBandwidthListener(self.tor))
        def set_result(result):
            self.stream_bandwidth_listener = result
        d.addCallback(set_result)

        class DummyResource(Resource):
            isLeaf = True

            def render_GET(self, request):
                return 'a'*8*2**20

        d.addCallback(lambda ign: available_tcp_port(reactor))
        def set_port(result):
            self.port = result
            self.server_endpoint = serverFromString(reactor, "tcp:interface=127.0.0.1:%s" % self.port)
            self.site = ThrottlingFactory(Site(DummyResource()), readLimit=1, writeLimit=1)
            self.listen_d = self.server_endpoint.listen(self.site)
            return self.listen_d
        d.addCallback(set_port)
        def is_connected(ignore):
            print "meow1"
            return None
        d.addCallback(is_connected)
        def fail_connect(f):
            print "connect FAIL"
            return f
        d.addErrback(fail_connect)
        return d

    @defer.inlineCallbacks
    def test_circ_bw(self):
        r = yield self.do_fetch()
        print "CIRC %s" % (r['circ'],)
        bw_events = self.stream_bandwidth_listener.circ_bw_events.get(r['circ'])
        import time
        time.sleep(2)
        print "events %s" % (bw_events,)
        assert bw_events
        # XXX: why are the counters reversed!? -> See StreamBandwidthListener
        #      docstring.
        # assert self.fetch_size/2 <= sum([x[1] for x in bw_events]) <= self.fetch_size
        assert sum([x[1] for x in bw_events]) <= self.fetch_size
        # either this is backward, or we wrote more bytes than read?!
        assert sum([x[2] for x in bw_events]) >= sum([x[1] for x in bw_events])

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
        assert len(bw_events) > 1
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
        assert len(bw_events) > 1

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
        self.listen_d.cancel()
