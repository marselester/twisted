# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
The WebSockets Protocol, according to RFC 6455
(http://tools.ietf.org/html/rfc6455). When "RFC" is mentioned, it refers to
this RFC. Some tests reference HyBi-10
(http://tools.ietf.org/html/draft-ietf-hybi-thewebsocketprotocol-10) or
HyBi-07 (http://tools.ietf.org/html/draft-ietf-hybi-thewebsocketprotocol-07),
which are drafts of RFC 6455.
"""

from zope.interface.verify import verifyObject

from twisted.trial.unittest import TestCase

from twisted.internet.protocol import Factory, Protocol
from twisted.python import log
from twisted.test.proto_helpers import StringTransportWithDisconnection
from twisted.protocols.policies import ProtocolWrapper

from twisted.web.http_headers import Headers
from twisted.web.resource import IResource, Resource
from twisted.web.server import NOT_DONE_YET, Request
from twisted.web.websockets import (
    CONTROLS, _makeAccept, _mask, _makeFrame, _parseFrames, _WSException,
    WebSocketsResource, WebSocketsProtocol, IWebSocketsResource)
from twisted.web.test.test_web import DummyRequest, DummyChannel



class TestFrameHelpers(TestCase):
    """
    Test functions helping building and parsing WebSockets frames.
    """

    def test_makeAcceptRFC(self):
        """
        L{_makeAccept} makes responses according to the RFC.
        """
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        self.assertEqual(_makeAccept(key), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")


    def test_maskNoop(self):
        """
        Blank keys perform a no-op mask.
        """
        key = "\x00\x00\x00\x00"
        self.assertEqual(_mask("Test", key), "Test")


    def test_maskNoopLong(self):
        """
        Blank keys perform a no-op mask regardless of the length of the input.
        """
        key = "\x00\x00\x00\x00"
        self.assertEqual(_mask("LongTest", key), "LongTest")


    def test_maskNoopOdd(self):
        """
        Masking works even when the data to be masked isn't a multiple of four
        in length.
        """
        key = "\x00\x00\x00\x00"
        self.assertEqual(_mask("LongestTest", key), "LongestTest")


    def test_maskHello(self):
        """
        A sample mask for "Hello" according to RFC 6455, 5.7.
        """
        key = "\x37\xfa\x21\x3d"
        self.assertEqual(_mask("Hello", key), "\x7f\x9f\x4d\x51\x58")


    def test_parseUnmaskedText(self):
        """
        A sample unmasked frame of "Hello" from HyBi-10, 4.7.
        """
        frame = ["\x81\x05Hello"]
        frames = list(_parseFrames(frame, needMask=False))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], (CONTROLS.TEXT, "Hello", True))
        self.assertEqual(frame, [])


    def test_parseUnmaskedLargeText(self):
        """
        L{_parseFrames} handles frame with text longer than 125 bytes.
        """
        frame = ["\x81\x7e\x00\xc8", "x" * 200]
        frames = list(_parseFrames(frame, needMask=False))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], (CONTROLS.TEXT, "x" * 200, True))
        self.assertEqual(frame, [])


    def test_parseUnmaskedHugeText(self):
        """
        L{_parseFrames} handles frame with text longer than 64 kB.
        """
        frame = ["\x81\x7f\x00\x00\x00\x00\x00\x01\x86\xa0", "x" * 100000]
        frames = list(_parseFrames(frame, needMask=False))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], (CONTROLS.TEXT, "x" * 100000, True))
        self.assertEqual(frame, [])


    def test_parseMaskedText(self):
        """
        A sample masked frame of "Hello" from HyBi-10, 4.7.
        """
        frame = ["\x81\x857\xfa!=\x7f\x9fMQX"]
        frames = list(_parseFrames(frame))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], (CONTROLS.TEXT, "Hello", True))
        self.assertEqual(frame, [])


    def test_parseMaskedPartialText(self):
        """
        L{_parseFrames} stops parsing if a masked frame isn't long enough to
        contain the length of the text.
        """
        frame = ["\x81\x827\xfa"]
        frames = list(_parseFrames(frame))
        self.assertEqual(len(frames), 0)
        self.assertEqual(frame, ["\x81\x827\xfa"])


    def test_parseUnmaskedTextFragments(self):
        """
        Fragmented masked packets are handled.

        From HyBi-10, 4.7.
        """
        frame = ["\x01\x03Hel\x80\x02lo"]
        frames = list(_parseFrames(frame, needMask=False))
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0], (CONTROLS.TEXT, "Hel", False))
        self.assertEqual(frames[1], (CONTROLS.CONTINUE, "lo", True))
        self.assertEqual(frame, [])


    def test_parsePing(self):
        """
        Ping packets are decoded.

        From HyBi-10, 4.7.
        """
        frame = ["\x89\x05Hello"]
        frames = list(_parseFrames(frame, needMask=False))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], (CONTROLS.PING, "Hello", True))
        self.assertEqual(frame, [])


    def test_parsePong(self):
        """
        Pong packets are decoded.

        From HyBi-10, 4.7.
        """
        frame = ["\x8a\x05Hello"]
        frames = list(_parseFrames(frame, needMask=False))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0], (CONTROLS.PONG, "Hello", True))
        self.assertEqual(frame, [])


    def test_parseCloseEmpty(self):
        """
        A HyBi-07 close packet may have no body. In that case, it decodes with
        the generic error code 1000, and has no particular justification or
        error message.
        """
        frame = ["\x88\x00"]
        frames = list(_parseFrames(frame, needMask=False))
        self.assertEqual(len(frames), 1)
        self.assertEqual(
            frames[0], (CONTROLS.CLOSE, (1000, "No reason given"), True))
        self.assertEqual(frame, [])


    def test_parseCloseReason(self):
        """
        A HyBi-07 close packet must have its first two bytes be a numeric
        error code, and may optionally include trailing text explaining why
        the connection was closed.
        """
        frame = ["\x88\x0b\x03\xe8No reason"]
        frames = list(_parseFrames(frame, needMask=False))
        self.assertEqual(len(frames), 1)
        self.assertEqual(
            frames[0], (CONTROLS.CLOSE, (1000, "No reason"), True))
        self.assertEqual(frame, [])


    def test_parsePartialNoLength(self):
        """
        Partial frames are stored for later decoding.
        """
        frame = ["\x81"]
        frames = list(_parseFrames(frame, needMask=False))
        self.assertEqual(len(frames), 0)
        self.assertEqual(frame, ["\x81"])


    def test_parsePartialTruncatedLengthInt(self):
        """
        Partial frames are stored for later decoding, even if they are cut on
        length boundaries.
        """
        frame = ["\x81\xfe"]
        frames = list(_parseFrames(frame, needMask=False))
        self.assertEqual(len(frames), 0)
        self.assertEqual(frame, ["\x81\xfe"])


    def test_parsePartialTruncatedLengthDouble(self):
        """
        Partial frames are stored for later decoding, even if they are marked
        as being extra-long.
        """
        frame = ["\x81\xff"]
        frames = list(_parseFrames(frame, needMask=False))
        self.assertEqual(len(frames), 0)
        self.assertEqual(frame, ["\x81\xff"])


    def test_parsePartialNoData(self):
        """
        Partial frames with full headers but no data are stored for later
        decoding.
        """
        frame = ["\x81\x05"]
        frames = list(_parseFrames(frame, needMask=False))
        self.assertEqual(len(frames), 0)
        self.assertEqual(frame, ["\x81\x05"])


    def test_parsePartialTruncatedData(self):
        """
        Partial frames with full headers and partial data are stored for later
        decoding.
        """
        frame = ["\x81\x05Hel"]
        frames = list(_parseFrames(frame, needMask=False))
        self.assertEqual(len(frames), 0)
        self.assertEqual(frame, ["\x81\x05Hel"])


    def test_parseReservedFlag(self):
        """
        L{_parseFrames} raises a L{_WSException} error when the header uses a
        reserved flag.
        """
        frame = ["\x72\x05"]
        error = self.assertRaises(_WSException, list, _parseFrames(frame))
        self.assertEqual("Reserved flag in frame (114)", str(error))


    def test_parseUnknownOpcode(self):
        """
        L{_parseFrames} raises a L{_WSException} error when the error uses an
        unknown opcode.
        """
        frame = ["\x8f\x05"]
        error = self.assertRaises(_WSException, list, _parseFrames(frame))
        self.assertEqual("Unknown opcode 15 in frame", str(error))


    def test_makeHello(self):
        """
        L{_makeFrame} makes valid HyBi-07 packets.
        """
        frame = "\x81\x05Hello"
        buf = _makeFrame("Hello", CONTROLS.TEXT, True)
        self.assertEqual(frame, buf)


    def test_makeLargeFrame(self):
        """
        L{_makeFrame} prefixes the payload by the length on 2 bytes if the
        payload is more than 125 bytes.
        """
        frame = "\x81\x7e\x00\xc8" + "x" * 200
        buf = _makeFrame("x" * 200, CONTROLS.TEXT, True)
        self.assertEqual(frame, buf)


    def test_makeHugeFrame(self):
        """
        L{_makeFrame} prefixes the payload by the length on 8 bytes if the
        payload is more than 64 kB.
        """
        frame = "\x81\x7f\x00\x00\x00\x00\x00\x01\x86\xa0" + "x" * 100000
        buf = _makeFrame("x" * 100000, CONTROLS.TEXT, True)
        self.assertEqual(frame, buf)


    def test_makeNonFinFrame(self):
        """
        L{_makeFrame} can build fragmented frames.
        """
        frame = "\x01\x05Hello"
        buf = _makeFrame("Hello", CONTROLS.TEXT, False)
        self.assertEqual(frame, buf)


    def test_makeMaskedFrame(self):
        """
        L{_makeFrame} can build masked frames.
        """
        frame = "\x81\x857\xfa!=\x7f\x9fMQX"
        buf = _makeFrame("Hello", CONTROLS.TEXT, True, mask="7\xfa!=")
        self.assertEqual(frame, buf)



class SavingEcho(WebSocketsProtocol):
    """
    A test protocol saving the data received and sending it back.
    """

    def connectionMade(self):
        WebSocketsProtocol.connectionMade(self)
        self.received = []


    def frameReceived(self, opcode, data, fin):
        self.received.append((opcode, data, fin))
        self.sendFrame(opcode, data, fin)



class WebsocketsProtocolTest(TestCase):

    def setUp(self):

        class SavingEchoFactory(Factory):

            def buildProtocol(oself, addr):
                return self.protocol

        self.protocol = SavingEcho()
        self.factory = SavingEchoFactory()
        self.transport = StringTransportWithDisconnection()
        self.protocol.makeConnection(self.transport)
        self.transport.protocol = self.protocol


    def test_frameReceived(self):
        """
        C{_WebSocketsProtocol.dataReceived} translates bytes into frames, and
        then write it back encoded into frames.
        """
        self.protocol.dataReceived(
            _makeFrame("Hello", CONTROLS.TEXT, True, mask="abcd"))
        self.assertEqual("\x81\x05Hello", self.transport.value())
        self.assertEqual([(CONTROLS.TEXT, "Hello", True)],
                         self.protocol.received)


    def test_ping(self):
        """
        When a C{PING} frame is received, the frame is resent with a C{PONG},
        but the application protocol doesn't receive any data.
        """
        self.protocol.dataReceived(
            _makeFrame("Hello", CONTROLS.PING, True, mask="abcd"))
        self.assertEqual("\x8a\x05Hello", self.transport.value())
        self.assertEqual([], self.protocol.received)


    def test_close(self):
        """
        When a C{CLOSE} frame is received, the protocol closes the connection
        and logs a message.
        """
        loggedMessages = []

        def logConnectionLostMsg(eventDict):
            loggedMessages.append(log.textFromEventDict(eventDict))

        log.addObserver(logConnectionLostMsg)

        self.protocol.dataReceived(
            _makeFrame("", CONTROLS.CLOSE, True, mask="abcd"))
        self.assertFalse(self.transport.connected)
        self.assertEqual(["Closing connection: 'No reason given' (1000)"],
                         loggedMessages)


    def test_invalidFrame(self):
        """
        If an invalid frame is received, C{_WebSocketsProtocol} closes the
        connection and logs an error.
        """
        self.protocol.dataReceived("\x72\x05")
        self.assertFalse(self.transport.connected)
        [error] = self.flushLoggedErrors(_WSException)
        self.assertEqual("Reserved flag in frame (114)", str(error.value))



class WebsocketsResourceTest(TestCase):

    def setUp(self):

        class SavingEchoFactory(Factory):

            def buildProtocol(oself, addr):
                return self.echoProtocol

        factory = SavingEchoFactory()
        self.echoProtocol = SavingEcho()

        self.resource = WebSocketsResource(factory)


    def assertRequestFail(self, request):
        """
        Helper method checking that the provided C{request} fails with a I{400}
        request code, without data or headers.

        @param request: The request to render.
        @type request: L{DummyRequest}
        """
        result = self.resource.render(request)
        self.assertEqual("", result)
        self.assertEqual({}, request.outgoingHeaders)
        self.assertEqual([], request.written)
        self.assertEqual(400, request.responseCode)


    def test_getChildWithDefault(self):
        """
        L{WebSocketsResource.getChildWithDefault} raises a C{RuntimeError} when
        called.
        """
        self.assertRaises(
            RuntimeError, self.resource.getChildWithDefault, "foo",
            DummyRequest("/"))


    def test_putChild(self):
        """
        L{WebSocketsResource.putChild} raises C{RuntimeError} when called.
        """
        self.assertRaises(
            RuntimeError, self.resource.putChild, "foo", Resource())


    def test_IResource(self):
        """
        L{WebSocketsResource} implements L{IResource} and
        L{IWebSocketsResource}.
        """
        self.assertTrue(verifyObject(IResource, self.resource))
        self.assertTrue(verifyObject(IWebSocketsResource, self.resource))


    def test_render(self):
        """
        When rendering a request, L{WebSocketsResource}, checks the C{Upgrade},
        C{Connection} and C{Sec-WebSocket-Version} headers, and use the
        C{Sec-WebSocket-Key} header to generate a C{Sec-WebSocket-Accept}
        value. It creates a L{_WebSocketsProtocol} instance connected to the
        protocol provided by the user factory.
        """
        request = DummyRequest("/")
        request.requestHeaders = Headers()
        transport = StringTransportWithDisconnection()
        request.transport = transport
        request.isSecure = lambda: False
        request.headers.update({
            "upgrade": "Websocket",
            "connection": "Upgrade",
            "sec-websocket-key": "secure",
            "sec-websocket-version": "13"})
        result = self.resource.render(request)
        self.assertEqual(NOT_DONE_YET, result)
        self.assertEqual(
            {"connection": "Upgrade",
             "upgrade": "WebSocket",
             "sec-websocket-accept": "oYBv54i42V5dw6KnZqOFroecUTc="},
            request.outgoingHeaders)
        self.assertEqual([""], request.written)
        self.assertEqual(101, request.responseCode)
        self.assertIdentical(None, request.transport)
        self.assertIsInstance(transport.protocol, SavingEcho)


    def test_renderProtocol(self):
        """
        If protocols are specified via the C{Sec-WebSocket-Protocol} header,
        L{WebSocketsResource} passes them to its C{lookupProtocol} method,
        which can decide which protocol to return, and which is accepted.
        """

        def lookupProtocol(names, otherRequest):
            self.assertEqual(["foo", "bar"], names)
            self.assertIdentical(request, otherRequest)
            return self.resource._factory.buildProtocol(None), "bar"

        self.resource.lookupProtocol = lookupProtocol

        request = DummyRequest("/")
        request.requestHeaders = Headers(
            {"sec-websocket-protocol": ["foo", "bar"]})
        transport = StringTransportWithDisconnection()
        request.transport = transport
        request.isSecure = lambda: False
        request.headers.update({
            "upgrade": "Websocket",
            "connection": "Upgrade",
            "sec-websocket-key": "secure",
            "sec-websocket-version": "13"})
        result = self.resource.render(request)
        self.assertEqual(NOT_DONE_YET, result)
        self.assertEqual(
            {"connection": "Upgrade",
             "upgrade": "WebSocket",
             "sec-websocket-protocol": "bar",
             "sec-websocket-accept": "oYBv54i42V5dw6KnZqOFroecUTc="},
            request.outgoingHeaders)
        self.assertEqual([""], request.written)
        self.assertEqual(101, request.responseCode)


    def test_renderWrongUpgrade(self):
        """
        If the C{Upgrade} header contains an invalid value,
        L{WebSocketsResource} returns a failed request.
        """
        request = DummyRequest("/")
        request.headers.update({
            "upgrade": "wrong",
            "connection": "Upgrade",
            "sec-websocket-key": "secure",
            "sec-websocket-version": "13"})
        self.assertRequestFail(request)


    def test_renderNoUpgrade(self):
        """
        If the C{Upgrade} header is not set, L{WebSocketsResource} returns a
        failed request.
        """
        request = DummyRequest("/")
        request.headers.update({
            "connection": "Upgrade",
            "sec-websocket-key": "secure",
            "sec-websocket-version": "13"})
        self.assertRequestFail(request)


    def test_renderPOST(self):
        """
        If the method is not C{GET}, L{WebSocketsResource} returns a failed
        request.
        """
        request = DummyRequest("/")
        request.method = "POST"
        request.headers.update({
            "upgrade": "Websocket",
            "connection": "Upgrade",
            "sec-websocket-key": "secure",
            "sec-websocket-version": "13"})
        self.assertRequestFail(request)


    def test_renderWrongConnection(self):
        """
        If the C{Connection} header contains an invalid value,
        L{WebSocketsResource} returns a failed request.
        """
        request = DummyRequest("/")
        request.headers.update({
            "upgrade": "Websocket",
            "connection": "Wrong",
            "sec-websocket-key": "secure",
            "sec-websocket-version": "13"})
        self.assertRequestFail(request)


    def test_renderNoConnection(self):
        """
        If the C{Connection} header is not set, L{WebSocketsResource} returns a
        failed request.
        """
        request = DummyRequest("/")
        request.headers.update({
            "upgrade": "Websocket",
            "sec-websocket-key": "secure",
            "sec-websocket-version": "13"})
        self.assertRequestFail(request)


    def test_renderNoKey(self):
        """
        If the C{Sec-WebSocket-Key} header is not set, L{WebSocketsResource}
        returns a failed request.
        """
        request = DummyRequest("/")
        request.headers.update({
            "upgrade": "Websocket",
            "connection": "Upgrade",
            "sec-websocket-version": "13"})
        self.assertRequestFail(request)


    def test_renderWrongVersion(self):
        """
        If the value of the C{Sec-WebSocket-Version} is not 13,
        L{WebSocketsResource} returns a failed request.
        """
        request = DummyRequest("/")
        request.headers.update({
            "upgrade": "Websocket",
            "connection": "Upgrade",
            "sec-websocket-key": "secure",
            "sec-websocket-version": "11"})
        result = self.resource.render(request)
        self.assertEqual("", result)
        self.assertEqual({"sec-websocket-version": "13"},
                         request.outgoingHeaders)
        self.assertEqual([], request.written)
        self.assertEqual(400, request.responseCode)


    def test_renderNoProtocol(self):
        """
        If the underlying factory doesn't return any protocol,
        L{WebSocketsResource} returns a failed request with a C{502} code.
        """
        request = DummyRequest("/")
        request.requestHeaders = Headers()
        request.transport = StringTransportWithDisconnection()
        self.echoProtocol = None
        request.headers.update({
            "upgrade": "Websocket",
            "connection": "Upgrade",
            "sec-websocket-key": "secure",
            "sec-websocket-version": "13"})
        result = self.resource.render(request)
        self.assertEqual("", result)
        self.assertEqual({}, request.outgoingHeaders)
        self.assertEqual([], request.written)
        self.assertEqual(502, request.responseCode)


    def test_renderSecureRequest(self):
        """
        When the rendered request is over HTTPS, L{WebSocketsResource} wraps
        the protocol of the C{TLSMemoryBIOProtocol} instance.
        """
        request = DummyRequest("/")
        request.requestHeaders = Headers()
        transport = StringTransportWithDisconnection()
        secureProtocol = ProtocolWrapper(Factory(), Protocol())
        transport.protocol = secureProtocol
        request.transport = transport
        request.isSecure = lambda: True
        request.headers.update({
            "upgrade": "Websocket",
            "connection": "Upgrade",
            "sec-websocket-key": "secure",
            "sec-websocket-version": "13"})
        result = self.resource.render(request)
        self.assertEqual(NOT_DONE_YET, result)
        self.assertEqual(
            {"connection": "Upgrade",
             "upgrade": "WebSocket",
             "sec-websocket-accept": "oYBv54i42V5dw6KnZqOFroecUTc="},
            request.outgoingHeaders)
        self.assertEqual([""], request.written)
        self.assertEqual(101, request.responseCode)
        self.assertIdentical(None, request.transport)
        self.assertIsInstance(transport.protocol.wrappedProtocol, SavingEcho)


    def test_renderRealRequest(self):
        """
        The request managed by L{WebSocketsResource.render} doesn't contain
        unnecessary HTTP headers like I{Content-Type} or I{Transfer-Encoding}.
        """
        channel = DummyChannel()
        channel.transport = StringTransportWithDisconnection()
        request = Request(channel, False)
        headers = {
            "upgrade": "Websocket",
            "connection": "Upgrade",
            "sec-websocket-key": "secure",
            "sec-websocket-version": "13"}
        for key, value in headers.items():
            request.requestHeaders.setRawHeaders(key, [value])
        request.method = "GET"
        request.clientproto = "HTTP/1.1"
        result = self.resource.render(request)
        self.assertEqual(NOT_DONE_YET, result)
        self.assertEqual(
            [("Connection", ["Upgrade"]),
             ("Upgrade", ["WebSocket"]),
             ("Sec-Websocket-Accept", ["oYBv54i42V5dw6KnZqOFroecUTc="])],
            list(request.responseHeaders.getAllRawHeaders()))
        self.assertEqual(
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Connection: Upgrade\r\n"
            "Upgrade: WebSocket\r\n"
            "Sec-Websocket-Accept: oYBv54i42V5dw6KnZqOFroecUTc=\r\n\r\n",
            channel.transport.value())
        self.assertEqual(101, request.code)
        self.assertIdentical(None, request.transport)
