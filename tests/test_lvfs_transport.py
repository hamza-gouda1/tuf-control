# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) Hamza Gouda
"""Pinned LVFS HTTPS transport using mocked sockets only; no network or downloads."""
import contextlib
import io
import socket
import ssl
import unittest
from unittest import mock

from asusius.updates import actions

PUBLIC = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))
PRIVATE = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))
URL = "https://fwupd.org/downloads/test.cab?revision=2"


class LvfsTransportTests(unittest.TestCase):
    def transport(self, response=b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\ncabinet"):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        dns = stack.enter_context(mock.patch.object(actions.socket, "getaddrinfo",
                                                    return_value=[PUBLIC]))
        raw = mock.Mock()
        sockets = stack.enter_context(mock.patch.object(actions.socket, "socket", return_value=raw))
        tls = mock.Mock()
        tls.makefile.return_value = io.BytesIO(response)
        context = mock.Mock()
        context.wrap_socket.return_value = tls
        default_context = stack.enter_context(mock.patch.object(actions.ssl, "create_default_context",
                                                                return_value=context))
        return dns, sockets, raw, tls, context, default_context

    def test_connects_to_vetted_address_with_original_hostname_tls_and_host_header(self):
        dns, sockets, raw, tls, context, default_context = self.transport()
        # A second resolution would yield a private address. It must never occur.
        dns.side_effect = [[PUBLIC], [PRIVATE]]
        watch = mock.Mock()
        self.assertEqual(actions._fetch_lvfs(URL, watch), b"cabinet")
        dns.assert_called_once_with("fwupd.org", 443, type=socket.SOCK_STREAM,
                                    proto=socket.IPPROTO_TCP)
        sockets.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        raw.connect.assert_called_once_with(PUBLIC[4])
        timeout = raw.settimeout.call_args.args[0]
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, actions.LVFS_CONNECT_TIMEOUT_S)
        default_context.assert_called_once_with()
        context.wrap_socket.assert_called_once_with(raw, server_hostname="fwupd.org")
        request = b"".join(call.args[0] for call in tls.sendall.call_args_list)
        self.assertIn(b"GET /downloads/test.cab?revision=2 HTTP/1.1\r\n", request)
        self.assertIn(b"Host: fwupd.org\r\n", request)
        self.assertIn(b"User-Agent: asusius\r\n", request)
        self.assertEqual(watch.phase.call_args.kwargs["percent"], 100)
        tls.close.assert_called_once_with()

    def test_mixed_or_empty_dns_answers_fail_before_connect(self):
        dns, sockets, *_ = self.transport()
        for answers in ([], [PRIVATE], [PUBLIC, PRIVATE]):
            with self.subTest(answers=answers):
                dns.return_value = answers
                with self.assertRaises(actions.ActionError):
                    actions._fetch_lvfs(URL)
        sockets.assert_not_called()

    def test_ipv6_sockaddr_is_used_without_resolving_it_again(self):
        dns, sockets, raw, _, context, _ = self.transport()
        address = ("2001:4860:4860::8888", 443, 0, 0)
        dns.return_value = [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address)]
        self.assertEqual(actions._fetch_lvfs(URL), b"cabinet")
        sockets.assert_called_once_with(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        raw.connect.assert_called_once_with(address)
        self.assertEqual(context.wrap_socket.call_args.kwargs["server_hostname"], "fwupd.org")
        dns.assert_called_once()

    def test_connection_failure_retries_only_other_vetted_addresses(self):
        dns, sockets, first, _, context, _ = self.transport()
        second = mock.Mock()
        second_addr = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 443))
        dns.return_value = [PUBLIC, second_addr]
        sockets.side_effect = [first, second]
        first.connect.side_effect = OSError("unreachable")
        self.assertEqual(actions._fetch_lvfs(URL), b"cabinet")
        first.close.assert_called_once_with()
        second.connect.assert_called_once_with(second_addr[4])
        context.wrap_socket.assert_called_once_with(second, server_hostname="fwupd.org")
        dns.assert_called_once()

    def test_connect_fallback_shares_one_deadline(self):
        dns, sockets, first, *_ = self.transport()
        second = mock.Mock()
        second_addr = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                       ("1.1.1.1", 443))
        dns.return_value = [PUBLIC, second_addr]
        sockets.side_effect = [first, second]
        clock = {"now": 100.0}

        def advance_past_connect_budget(_address):
            clock["now"] = 161.0
            raise OSError("connect timed out")

        first.connect.side_effect = advance_past_connect_budget
        with mock.patch.object(actions.time, "monotonic", side_effect=lambda: clock["now"]):
            with self.assertRaises(TimeoutError):
                actions._fetch_lvfs(URL)

        sockets.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM,
                                        socket.IPPROTO_TCP)
        first.close.assert_called_once_with()
        second.connect.assert_not_called()

    def test_tls_handshake_uses_only_the_remaining_connect_budget(self):
        _, _, raw, _, context, _ = self.transport()
        now = [0.0]
        raw.connect.side_effect = lambda _address: now.__setitem__(0, 59.0)
        with mock.patch.object(actions.time, "monotonic", side_effect=lambda: now[0]):
            self.assertEqual(actions._fetch_lvfs(URL), b"cabinet")
        self.assertEqual(raw.settimeout.call_args_list,
                         [mock.call(60), mock.call(1)])
        context.wrap_socket.assert_called_once()

    def test_expired_connect_budget_never_starts_tls(self):
        _, _, raw, _, context, _ = self.transport()
        now = [0.0]
        raw.connect.side_effect = lambda _address: now.__setitem__(0, 60.0)
        with mock.patch.object(actions.time, "monotonic", side_effect=lambda: now[0]):
            with self.assertRaises(TimeoutError):
                actions._fetch_lvfs(URL)
        context.wrap_socket.assert_not_called()
        raw.close.assert_called_once_with()

    def test_slow_trickle_cannot_exceed_overall_transfer_deadline(self):
        _, _, _, tls, _, _ = self.transport(
            b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nx")
        clock = {"now": 0.0}
        class SlowReader(io.BytesIO):
            def readinto(self, buffer):
                # Complete headers first, then trickle the body beyond the budget.
                if self.tell() == 0:
                    prefix = self.getvalue().index(b"\r\n\r\n") + 4
                    return super().readinto(memoryview(buffer)[:prefix])
                result = super().readinto(buffer)
                if result:
                    clock["now"] += actions.LVFS_TRANSFER_TIMEOUT_S + 1
                return result

        reader = SlowReader(b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nx")
        tls.makefile.return_value = reader
        with mock.patch.object(actions.time, "monotonic", side_effect=lambda: clock["now"]):
            with self.assertRaisesRegex(actions.ActionError, "180 second time limit"):
                actions._fetch_lvfs(URL)
        tls.close.assert_called_once_with()

    def test_status_and_header_trickles_share_the_absolute_deadline(self):
        prefix = b"HTTP/1.1 200 OK\r\n"
        headers = b"X-Slow: yes\r\nContent-Length: 1\r\n\r\nx"
        for fast_prefix in (b"", prefix):
            with self.subTest(phase="headers" if fast_prefix else "status"):
                _, _, _, tls, _, _ = self.transport()
                clock = {"now": 0.0}

                class TrickleReader(io.BytesIO):
                    def readinto(self, buffer):
                        if self.tell() < len(fast_prefix):
                            return super().readinto(memoryview(buffer)[:len(fast_prefix)])
                        clock["now"] += 59
                        return super().readinto(memoryview(buffer)[:1])

                reader = TrickleReader(prefix + headers)
                tls.makefile.return_value = reader
                with mock.patch.object(actions.time, "monotonic",
                                       side_effect=lambda: clock["now"]):
                    with self.assertRaisesRegex(actions.ActionError, "180 second time limit"):
                        actions._fetch_lvfs(URL)
                # A real socket times out after the remaining 3 seconds. Our fake
                # intentionally ignores it, so the post-read check catches overrun.
                self.assertEqual(clock["now"], 236)
                self.assertIn(mock.call(3), tls.settimeout.call_args_list)
                self.assertTrue(reader.closed)
                tls.makefile.assert_called_once_with("rb", buffering=0)
                tls.close.assert_called_once_with()

    def test_chunk_size_and_trailer_trickles_share_the_absolute_deadline(self):
        headers = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        for framing in (b"00000001\r\nx\r\n0\r\n\r\n",
                        b"0\r\nX-Trailer: slow\r\n\r\n"):
            with self.subTest(framing=framing):
                _, _, _, tls, _, _ = self.transport()
                clock = {"now": 0.0}

                class TrickleReader(io.BytesIO):
                    def readinto(self, buffer):
                        if self.tell() == 0:
                            return super().readinto(memoryview(buffer)[:len(headers)])
                        clock["now"] += 59
                        return super().readinto(memoryview(buffer)[:1])

                reader = TrickleReader(headers + framing)
                tls.makefile.return_value = reader
                with mock.patch.object(actions.time, "monotonic",
                                       side_effect=lambda: clock["now"]):
                    with self.assertRaisesRegex(actions.ActionError, "180 second time limit"):
                        actions._fetch_lvfs(URL)
                self.assertEqual(clock["now"], 236)
                self.assertIn(mock.call(3), tls.settimeout.call_args_list)
                self.assertTrue(reader.closed)
                tls.close.assert_called_once_with()

    def test_tls_verification_error_closes_socket_and_propagates(self):
        _, _, raw, _, context, _ = self.transport()
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError("wrong hostname")
        with self.assertRaises(ssl.SSLCertVerificationError):
            actions._fetch_lvfs(URL)
        raw.close.assert_called_once_with()

    def test_redirect_is_refused_and_connection_closed(self):
        dns, _, _, tls, _, _ = self.transport(
            b"HTTP/1.1 302 Found\r\nLocation: https://127.0.0.1/secret\r\nContent-Length: 0\r\n\r\n")
        with self.assertRaisesRegex(actions.ActionError, "redirect"):
            actions._fetch_lvfs(URL)
        dns.assert_called_once()
        tls.close.assert_called_once_with()

    def test_http_error_is_not_a_firmware_cabinet(self):
        _, _, _, tls, _, _ = self.transport(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        with self.assertRaisesRegex(actions.ActionError, "HTTP 404"):
            actions._fetch_lvfs(URL)
        tls.close.assert_called_once_with()

    def test_declared_and_streamed_size_limits_still_apply(self):
        for response in (b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\ncabinet",
                         b"HTTP/1.1 200 OK\r\n\r\ncabinet"):
            with self.subTest(response=response):
                _, _, _, tls, _, _ = self.transport(response)
                with mock.patch.object(actions, "MAX_FIRMWARE_BYTES", 3):
                    with self.assertRaisesRegex(actions.ActionError, "download limit"):
                        actions._fetch_lvfs(URL)
                tls.close.assert_called_once_with()

    def test_invalid_url_fails_before_dns_or_network(self):
        dns, sockets, *_ = self.transport()
        for url in ("http://fwupd.org/file.cab", "https://elsewhere.example/file.cab",
                    "https://user:password@fwupd.org/file.cab", "https://fwupd.org:8443/file.cab",
                    "https://fwupd.org/file.cab#fragment"):
            with self.subTest(url=url):
                with self.assertRaises(actions.ActionError):
                    actions._fetch_lvfs(url)
        dns.assert_not_called()
        sockets.assert_not_called()

    def test_proxy_environment_does_not_change_connection_destination(self):
        dns, _, raw, *_ = self.transport()
        with mock.patch.dict("os.environ", {"https_proxy": "http://127.0.0.1:9999",
                                             "HTTPS_PROXY": "http://127.0.0.1:9999"}):
            self.assertEqual(actions._fetch_lvfs(URL), b"cabinet")
        dns.assert_called_once()
        raw.connect.assert_called_once_with(PUBLIC[4])


if __name__ == "__main__":
    unittest.main()
