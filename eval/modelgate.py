"""Publish the harness's model endpoint onto a task's internal network. §2.5.7.

**The problem.** A harness inside a container cannot reach a model listening on the
host's loopback. Measured, every obvious route fails:

    container -> 127.0.0.1:8001            the container's own loopback
    container -> 172.17.0.1:8001           the bridge gateway; vLLM binds loopback only,
                                           so nothing is listening there
    container -> host.docker.internal      not defined on Linux by default
    internal  -> <its own gateway>:8001    same, nothing listening

**The wrong answer is `--network bridge`.** That would give the task the internet, and
"a task with network is a different task from one without" (§2.5.4) — an `exec` task
would quietly become a networked task because of how the platform delivered its model.

**The answer is to publish the endpoint on the network the task already has.** The
load-bearing fact, measured on this host: a container on an `--internal` network *can*
reach a host process bound to that network's gateway address, and still cannot reach
the internet. So the platform runs a host-side forwarder on that gateway and hands the
harness its address instead of the loopback one.

That keeps three things true at once, which is why it is worth the moving part:

* the harness gets a model, without which there is nothing to measure;
* the task keeps whatever network it declared — `none` and `services` both stay
  offline;
* `env.model_gateway` records that this happened, because "the model was reachable
  from inside the container" is a fact about the measurement and not a detail.
"""
from __future__ import annotations

import socket
import socketserver
import threading
import urllib.parse


class GatewayUnavailable(RuntimeError):
    """The endpoint could not be published onto the task's network."""


# ---------------------------------------------------------------- 已知限制 ---
#
# **这个网关不理解 HTTP,所以它会把上游的空闲关闭传导给客户端。** 实测并复现
# (2026-10-02,容器内、`python:3.11-slim`、`http.client` 默认 keep-alive):
#
#     call 1: HTTP 200
#     sleeping 10s ...
#     call 2: FAILED RemoteDisconnected: Remote end closed connection without response
#
# 机制:vLLM 的 uvicorn 默认 `--timeout-keep-alive 5`,5 秒没有请求就关掉那条连接;
# `_pump` 读到上游 EOF 后 `shutdown(SHUT_WR)` **半关掉客户端**;客户端的下一次请求
# 复用那条已经死掉的连接 → `RemoteDisconnected` → `openai` 报
# `APIConnectionError: Connection error`。
#
# 代价在测量上是具体的:那一次运行里 `bn-fit-modify`(中间跑 pandas)和
# `chess-best-move`(中间跑 PIL)都死在这上面,而平台最初把它们记成了 **0.0** ——
# 一次网关故障被读成"harness 很弱"。
#
# 两侧各修了一半,而且都不需要这个网关理解 HTTP:
#
#   * **harness 侧(`base_harness/loop/agent.py`)**:每次请求用一条新连接
#     (`Connection: close`)。已验证:同一链路、同样隔 10 秒、三次调用全部 200。
#   * **平台侧(`eval/runner.py`)**:harness 因连不上模型端点而死时,这道题记成
#     `invalid`(stage/service 点名),不再记成 0 —— 见 `_provider_failure`。
#
# 仍然没修的是**根**:"上游关闭 -> 传导给客户端"这条路径还在,所以任何**不复用连接
# 之外**的 keep-alive 交互仍可能踩到。要根治,这个网关必须解析 HTTP,知道一次响应
# 在哪里结束、下一次请求从哪里开始,才能在上游空闲关闭时只丢上游、不动客户端。
# 那是一个真代理,不是几十行泵;在它存在之前,`Connection: close` 是那条便宜的、
# 已经被验证过的路。


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            chunk = src.recv(65536)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        # Half-close rather than close: the reply may still be in flight the other way,
        # and a hard close here truncates responses on any request larger than one read.
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        target = self.server.target                      # type: ignore[attr-defined]
        try:
            upstream = socket.create_connection(target, timeout=15)
        except OSError:
            # The model is down or unreachable. Closing the connection is the honest
            # answer: the harness sees a connection error, which is what happened.
            return
        with upstream:
            both = [threading.Thread(target=_pump, args=(self.request, upstream)),
                    threading.Thread(target=_pump, args=(upstream, self.request))]
            for t in both:
                t.daemon = True
                t.start()
            for t in both:
                t.join()


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, target):
        self.target = target
        super().__init__(address, handler)


class ModelGateway:
    """A forwarder from one host address:port to the agent's real model endpoint."""

    def __init__(self, *, bind_host: str, target: tuple[str, int], path: str = "",
                 port: int = 0):
        self.bind_host = bind_host
        self.target = target
        self.path = path
        #: 0 means "the platform picks". A fixed port is for the *other* caller of this
        #: pump -- `tools/egress_forwarder.py` publishes a proxy that containers are told
        #: about by address, and an address whose port changes every restart is not
        #: something an operator can put in `.env`.
        self.port = port
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "ModelGateway":
        try:
            # Port 0: the platform picks, because a fixed port would collide between
            # concurrent runs and would have to be recorded to be meaningful anyway.
            self._server = _Server((self.bind_host, self.port), _Handler, self.target)
        except OSError as exc:
            raise GatewayUnavailable(
                f"could not bind a model gateway on {self.bind_host}: {exc}") from exc
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def base_url(self) -> str:
        """What the harness should be handed, path preserved.

        The path matters: `.../v1` is part of an OpenAI-compatible endpoint and dropping
        it would produce a 404 that reads like a wrong model rather than a wrong URL.
        """
        return f"http://{self.bind_host}:{self.port}{self.path}"

    def stop(self) -> None:
        """Never raises: it runs in a `finally` and must not replace the real failure."""
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except OSError:
                pass
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def is_host_local(url: str) -> bool:
    """True when `url` points at the host running the platform.

    Deliberately a small, explicit list rather than a DNS resolution: resolving here
    would make the decision depend on the platform's own resolver, and the failure mode
    is publishing a gateway for an endpoint that was reachable directly anyway.
    """
    host = urllib.parse.urlparse(url).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal")


def split(url: str) -> tuple[tuple[str, int], str]:
    """`(host, port)` and the path, from a base URL. Raises on anything unusable."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.hostname:
        raise GatewayUnavailable(f"cannot publish {url!r}: it has no host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return (parsed.hostname, port), (parsed.path or "")
