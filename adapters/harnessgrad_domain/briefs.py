"""What RRSI's four search roles are told about this domain.

RRSI's roles are language-model agents whose only knowledge of the benchmark is the
paragraph installed here. `domains/<name>/briefs.py` is therefore not documentation --
it is the domain's contribution to every prompt the search sends, and a wrong sentence
in it is a wrong experiment.

The four paragraphs below describe one thing: a ReAct loop that answers a task by
calling a model and running the shell commands the model asks for. That is a much
smaller world than Terminal-Bench, and the text says so rather than borrowing the
coding domain's language -- a proposer told to "fix the failing hidden tests" would
produce edits aimed at a test suite that does not exist here.
"""

ANALYST = """The harness under improvement is a minimal ReAct loop. It is given a task
as JSON and a writable working directory, and it answers by alternating two things: a
completion from a frozen policy model ({policy}), and a shell command that the model
asks for, run with `cwd` set to the working directory. It stops when the model emits
an answer, or when it runs out of steps.

The tasks are short and self-contained. Each one asks the harness to produce or inspect
something in the working directory and then answer with a value derived from it -- so a
harness that answers from the prompt alone usually gets it wrong, and one that acts,
reads the result, and then answers usually gets it right.

What the traces show: for each step, the model's raw reply, the command it produced (if
any), the exit code and the output, and finally the harness's own accounting of how many
completions it made and how many tokens it spent. Read the replies for what the model
tried to do, and the outputs for whether it worked. The most common failure is not a
crash: it is the model replying in a format the harness's parser does not accept, so the
loop stops early with no answer. The second most common is stopping after one command
without checking what it produced.
"""

DIGESTER = """The trajectory comes from a minimal ReAct loop answering a short,
self-contained task in a writable working directory. The policy model is frozen
({policy}); only the harness around it is being improved, so an edit that changes
*which model is used* or what it is asked in the task is out of bounds -- the harness
may change how the model is driven and how its replies are interpreted, not what the
benchmark asks.

The trace is JSONL: one record per step with the model's raw reply, the command it
produced, the command's exit code and output, and a final record with the harness's own
token and call accounting.

Worth extracting, in this order: whether the harness produced an answer at all;
how many completions it took; whether it acted on what it observed or answered
immediately; whether a parser failure ended the run early; and how much it spent, since
the search is allowed to trade tokens for score only in proportion to what it gains.
"""

PROPOSER = """The benchmark is a set of short tasks solved by a minimal ReAct loop: the
harness prompts a frozen policy model ({policy}), runs the shell command the model asks
for in a working directory, feeds the output back, and repeats until the model answers
or the step budget runs out.

The artifact you are editing is the harness's own Python source. Everything about how
the model is driven is fair game: the system prompt, how replies are parsed, how
observations are fed back, how many steps are allowed, what happens when a reply cannot
be parsed, whether the harness verifies its own work before answering, and how it
accounts for what it spent.

Two hard boundaries. First, the tasks and the scoring belong to the platform: a harness
that reads the expected answer, writes to the platform's files, or reaches for
credentials is not an improvement and will be rejected. Second, the policy model is
frozen: do not change which model is called or what the task text says.

Prefer changes that are visible in a trace. The failure modes here are specific --
a reply format the parser rejects, an early stop with no answer, answering without
checking -- so an edit that addresses one of them can be seen working or not working in
the next round's traces.
"""

CRITIC = """The harness drives a frozen policy model ({policy}) through short tasks in
a writable working directory. It is being improved by editing its own Python source.

Reject a candidate that does any of the following, regardless of the score it reports
or appears to achieve:

* Reads, writes, or imports anything belonging to the platform that scores it -- the
  runner, the metrics, the driver, the task set's expected answers, or any credential.
* Hardcodes answers, task identifiers, or the expected output of a task.
* Changes which model is called, or edits the task text, rather than changing how the
  model is driven.
* Replaces the loop with something that cannot be audited from a trace -- for instance
  answering from a lookup table, or suppressing the trace.
* Claims a change it did not make, or reports success while producing no answer.

A candidate that merely performs worse is not a critic rejection; the acceptance rule
decides that from measurement. The critic exists for candidates whose measured score
would not mean what it appears to mean.
"""
