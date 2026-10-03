# Experiment: what a harness's parser is worth

A controlled comparison on a single model, changing nothing but the harness's
`_parse` function. Run with `data/probe_set.py` (6 tasks), one round, no method
(`methods/noop/run.py`), against a locally served **Qwen3.5-9B** through vLLM.

## Result

| harness | score | commands run | parse errors | tokens |
| --- | --- | --- | --- | --- |
| `base_harness/loop` (strict `json.loads`) | **0.167** | 3 | **5** | 2,876 |
| `base_harness/loop_rrsi_parse` (tolerant) | **0.833** | 15 | **0** | 6,655 |

Per task, original harness:

```
d01   0 commands, 1 parse error    <- failed before doing anything
d02   0 commands, 1 parse error
d03   0 commands, 1 parse error
d04   0 commands, 1 parse error
d05   0 commands, 1 parse error
d06   3 commands, 0 parse errors   <- the one that passed
```

Per task, tolerant harness:

```
d01   2 commands    d02   2 commands    d03   2 commands
d04   1 command     d05   6 commands    d06   2 commands
```

## What actually failed

The model replies with a well-formed action object followed by one extra brace:

```json
{"tool": "bash", "command": "cat > calc.py << 'EOF'\ndef mul(a, b):\n    return a * b\nEOF"}}
                                                                                        ^ extra
```

`json.loads` rejects the whole reply with `Extra data: line 1 column N`, the harness
treats an unparsable action as terminal, and the episode ends with no answer and no
command executed. Five of six tasks died this way.

The tolerant version tries `raw_decode` at every `{` and keeps the first complete
object it can parse, so the trailing brace is irrelevant. It also strips markdown
fences, which this model emits sometimes.

## What this establishes

**1. The difference is 0.167 versus 0.833 at a fixed model.** The score moved by
+0.667 without touching the model, the tasks, or the sampling. Whatever one believes
about harness quality in the abstract, here it is worth five of six tasks.

**2. The failure is invisible in the score alone.** A curve showing 0.167 does not say
whether the model could not do the task or the harness could not read the answer. Only
the trace separates them: 5 parse errors and 0 commands is a harness defect, and it
looks exactly like a weak model from the outside. This is the case
`INTERFACE.md` §3 is built around -- the platform records `edit_kind` and the trace so
that the difference is recoverable afterwards.

**3. Tolerating a format is not the same as fixing the model.** The tolerant parser
does not make the model better; it stops the harness from discarding work the model
already did. Note the cost: the winning harness spends **2.3x the tokens**, because it
actually runs the commands the failing one threw away. An acceptance rule that only
looked at score would call this an unambiguous win; RRSI's cost rule sees
`Delta C = +1.31` against `Delta S = +0.667` and admits it because
`beta0 + beta1 * Delta S = 0.1 + 40 * 0.667 = 26.8` covers it.

## Why a local model found this and the hosted one did not

`deepseek-flash` scores 0.500-0.833 on the same tasks with the same strict parser: it
usually emits exactly one object, so the bug rarely fires. Qwen3.5-9B appends a
trailing brace often enough that five of six tasks hit it.

A harness defect of this kind is a **latency**, not an absence -- it fires with some
probability per reply, and a weaker or differently-trained model raises that
probability. Two consequences worth keeping:

* A harness validated only against a strong hosted model can be carrying a defect that
  a local or smaller model exposes immediately. Testing against more than one model is
  not thoroughness for its own sake; it changes which bugs are visible.
* Conversely, a low score from a weak model is not evidence about the model until the
  traces show the harness delivered the model's work. The measurement is only as good
  as the harness's ability to be understood by what it is measuring.

## Reproducing

```bash
# server
tools/serve_local_model.sh --model /mnt/20t/qzs/PLMs/Qwen3.5-9B --port 8001 --name qwen3.5-9b-local
# or: vllm serve <model> --default-chat-template-kwargs '{"enable_thinking": false}'
#     (a reasoning model narrates before answering, which the harness cannot parse either)

# the comparison
export HG_AGENT_BACKEND=openai HG_AGENT_MODEL=qwen3.5-9b-local \
       HG_AGENT_BASE_URL=http://127.0.0.1:8001/v1 HG_AGENT_API_KEY=local
for h in loop loop_rrsi_parse; do
    python3 driver.py --harness $h --mode A --rounds 1 --run-id cmp-$h \
        --dataset probe_set --sampling all \
        --method-entrypoint "python $PWD/methods/noop/run.py"
done
python3 tools/plot_curve.py runs/cmp-loop runs/cmp-loop_rrsi_parse
```

`loop_rrsi_parse` is RRSI's round-0 candidate A, extracted from its own repository
(`domains/harnessgrad/harness/` at commit `a203332`) so the comparison is of the
method's actual output rather than of a reimplementation. Its `harness.json` was
renamed to keep the two distinguishable in the curve record.
