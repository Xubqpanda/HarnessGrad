"""连不上模型端点 ≠ harness 很弱。两者不能是同一个 0。

这是平台存在的理由本身(§2.5.7),而它真的发生过:一次运行里 `bn-fit-modify` 和
`chess-best-move` 的 harness 都死在

    RuntimeError: 3 attempts failed, last: Connection error.

而平台给它们记的是 **0.0** —— 一次网关故障被读成了"harness 做不出来"。

机制在容器里复现过(`eval/modelgate.py` 有完整说明):vLLM 的 uvicorn 默认 5 秒关掉
空闲 keep-alive,而平台的字节转发网关把上游的 EOF 半关到客户端,于是 harness 隔十几秒
的下一次调用复用了一条死连接。**keep-alive 是客户端的正常行为,所以这是网关的缺陷。**

这个文件测三件事:

  * 判据**窄**:只有"非零退出码 + stderr 尾部有连接痕迹"才算提供方故障;
  * 判成之后进 `invalid`(带 stage/service),**不进 `per_task`** —— 不进分数;
  * 判据的两侧都要能失败:退出码 0 的不算,连上了又自己重试成功的不算。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import eval.runner as runner  # noqa: E402

#: 真实那一次的 stderr 尾部(截取)。
CONNECTION_TAIL = '''  File "/app/agent.py", line 162, in _call_openai
    raise RuntimeError(f"{attempts} attempts failed, last: {last}") from last
RuntimeError: 3 attempts failed, last: Connection error.
'''

APICONN = '''  File "/overlays/3.12/openai/_base_client.py", line 1183, in request
    raise APIConnectionError(request=request) from err
openai.APIConnectionError: Connection error.
'''


def test_a_harness_that_died_on_a_connection_error_is_a_provider_failure():
    got = runner._provider_failure({"exit_code": 1, "stderr": CONNECTION_TAIL})
    assert got is not None
    assert got["marker"] == "Connection error"
    assert "Connection error" in got["line"]


def test_the_openai_client_s_own_wording_is_recognised_too():
    got = runner._provider_failure({"exit_code": 1, "stderr": APICONN})
    assert got is not None and got["marker"] == "APIConnectionError"


def test_a_harness_that_exited_zero_is_never_a_provider_failure():
    """跑完了就是跑完了:它的分数是它该得的。

    这一条防的是"把失败都推给基础设施"。一个连上又掉了、但自己重试成功的 harness
    会在 stderr 里留下同样的字眼 —— 而它做对了,不该被算成没测到。
    """
    assert runner._provider_failure({"exit_code": 0, "stderr": CONNECTION_TAIL}) is None


def test_a_transient_error_the_harness_recovered_from_is_not_a_provider_failure():
    """只在**尾部**找痕迹:中间的、被它自己吸收掉的错误不算。"""
    recovered = ("Connection error while calling the model; retrying\n" * 20
                 + "answer written\n")
    assert runner._provider_failure({"exit_code": 0, "stderr": recovered}) is None


def test_an_unrelated_failure_is_not_misattributed():
    """harness 因为自己的 bug 死了,就不能赖到网关头上。"""
    assert runner._provider_failure(
        {"exit_code": 1, "stderr": "KeyError: 'entrypoint'\n"}) is None
    assert runner._provider_failure({"exit_code": None, "stderr": ""}) is None


def test_the_invalid_record_names_the_stage_and_the_service():
    """`invalid` 的全部内容是归因:读者下一个问题就是"谁的错",而答案不是 harness。"""
    result = {"exit_code": 1, "stderr": CONNECTION_TAIL,
              "model_gateway": {"published": True, "target": "127.0.0.1:8001"}}
    provider = runner._provider_failure(result)
    assert provider is not None
    # 这两句是 `_evaluate` 里写进 invalid 的内容,单独断言,免得措辞漂了没人发现。
    stage = "harness model call"
    detail = (f"the harness could not reach the model endpoint and exited "
              f"{provider['exit_code']}: {provider['line']}")
    assert "model endpoint" in detail and "Connection error" in detail
    assert stage == "harness model call"


# ------------------------------- 判据接进真实评估路径(它以前从没被调用过) ---

def _death(**over) -> dict:
    """一次"harness 死在上游连不上"的结果,字段与 `run_one` 返回的同名。"""
    result = {"task_id": "t1", "answer": "", "trace": "", "exit_code": 1,
              "stderr": CONNECTION_TAIL,
              "verdict": {"kind": "command", "passed": False, "score": 0.0,
                          "detail": "reward 0 from /logs/verifier/reward.txt"},
              "env_usage": {}, "model_gateway": {"published": True,
                                                 "target": "127.0.0.1:8001",
                                                 "connections": 3,
                                                 "upstream_connect_failed": 1,
                                                 "upstream_connect_error":
                                                     "ConnectionRefusedError: [Errno 111]",
                                                 "upstream_closed_first": 2,
                                                 "client_closed_first": 0,
                                                 "bytes_to_upstream": 0,
                                                 "bytes_to_client": 0,
                                                 "read_errors": []},
              "identity": None}
    result.update(over)
    return result


def test_a_provider_death_reaches_the_curve_as_invalid(tmp_path, monkeypatch):
    """**这条之前不存在,而它是这个文件存在的理由。**

    `_provider_failure` 写好了、测过了,然后**没有任何生产路径调用它** —— 判据只活在
    它自己的测试里。于是一次 91 个 task-run 里死了 20 个的运行,在曲线上还是普通非零
    退出。这里钉住的是接线本身:死因进 `invalid`、带 stage、不进 `per_task`。
    """
    monkeypatch.setattr(runner, "run_one", lambda *_a, **_k: _death())
    res = runner._evaluate(tmp_path, [{"task_id": "t1"}], {}, trials=1)
    assert res["per_task"] == {}, "一次网关故障不能变成 0 分"
    bad = res["invalid"]["t1"]
    assert bad["stage"] == "harness model call"
    assert "could not reach the model endpoint" in bad["detail"]
    assert "Connection error" in bad["detail"], "要留下哪一句"
    assert "the model hung up first" in bad["detail"], "网关自己的账也要在记录里"


def test_a_passing_artifact_survives_the_death_of_its_harness(tmp_path, monkeypatch):
    """写完 artifact 之后才死的 harness 完成了这道题(§2.2):分数保留。"""
    monkeypatch.setattr(runner, "run_one", lambda *_a, **_k: _death(
        verdict={"kind": "command", "passed": True, "score": 1.0, "detail": "ok"}))
    res = runner._evaluate(tmp_path, [{"task_id": "t1"}], {}, trials=1)
    assert res["per_task"] == {"t1": 1.0}
    assert res["invalid"] == {}


def test_a_trial_that_measured_survives_a_later_connection_death(tmp_path, monkeypatch):
    """`--trials` 的意义就是把这种噪声平均掉,而不是让第二个 trial 抹掉第一个。"""
    calls = {"n": 0}

    def fake(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _death(exit_code=0, stderr="", model_gateway=None)
        return _death()

    monkeypatch.setattr(runner, "run_one", fake)
    res = runner._evaluate(tmp_path, [{"task_id": "t1"}], {}, trials=2)
    assert res["invalid"] == {}, "已经测到的一次不能被后来的连接故障抹掉"
    assert res["per_task"] == {"t1": 0.0}
    assert res["per_task_trials"]["t1"] == [0.0]


# ------------------------------------------------- 网关自己的账(真 socket) ---

def _poll(gw, key, want, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        account = gw.account()
        if account.get(key) == want:
            return account
        time.sleep(0.02)
    return gw.account()


def test_the_gateway_counts_who_hung_up_first():
    """上游空闲关闭 → 网关把 EOF 传给客户端,这一次连接记在 upstream 头上。

    这就是 `bn-fit-modify` / `chess-best-move` 那次的机制(`eval/modelgate.py` 顶部
    有完整复现):harness 的下一次调用复用了一条死连接,而客户端看到的只有
    `Connection error`。转发器不懂 HTTP,所以现在还修不了根 —— 但至少要记账。
    """
    import socket
    import threading

    import eval.modelgate as modelgate

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve():
        conn, _ = listener.accept()
        conn.close()                      # 上游挂断,一个字节都不回

    threading.Thread(target=serve, daemon=True).start()
    gw = modelgate.ModelGateway(bind_host="127.0.0.1",
                                target=("127.0.0.1", listener.getsockname()[1])).start()
    try:
        client = socket.create_connection(("127.0.0.1", gw.port), timeout=5)
        assert client.recv(10) == b"", "客户端必须看到连接结束(它看到的就是那个 error)"
        client.close()
        account = _poll(gw, "upstream_closed_first", 1)
    finally:
        gw.stop()
        listener.close()
    assert account["connections"] == 1
    assert (account["upstream_closed_first"], account["client_closed_first"]) == (1, 0)
    assert "the model hung up first" in modelgate.describe(
        {"published": True, **account})


def test_the_gateway_says_why_it_could_not_reach_the_model():
    """模型根本没在听的时候,原因(`Connection refused`)以前在网关里被 `except: pass`
    吞掉:harness 报同一句 `Connection error`,记录里一个字的原因都没有。"""
    import socket

    import eval.modelgate as modelgate

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()                          # 没人监听这个端口了

    gw = modelgate.ModelGateway(bind_host="127.0.0.1",
                                target=("127.0.0.1", dead_port)).start()
    try:
        client = socket.create_connection(("127.0.0.1", gw.port), timeout=5)
        assert client.recv(10) == b""
        client.close()
        account = _poll(gw, "upstream_connect_failed", 1)
    finally:
        gw.stop()
    assert account["upstream_connect_failed"] == 1
    assert account["upstream_connect_error"], "原因必须留下来,哪怕是 Errno"
    assert "refus" in account["upstream_connect_error"].lower(), account
    note = modelgate.describe({"published": True, **account})
    assert "could not reach the model" in note and "Errno" in note


def test_a_record_with_no_gateway_attributes_nothing():
    """端点本来就直连的时候没有网关,不能被赖上 —— 空的归因比错的归因好。"""
    import eval.modelgate as modelgate

    assert modelgate.describe(None) == ""
    assert modelgate.describe({}) == ""
    assert modelgate.describe({"published": False}) == ""
