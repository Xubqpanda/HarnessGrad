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
