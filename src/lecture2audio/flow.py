"""The per-part scheduler: a graph of nodes, each run as soon as the nodes it needs are done.

A node is one (stage, part) pair of the declared stage graph (`stages.py`), a whole-lecture
stage, or a check that runs beside the main path (an audit). `add_part` and `add_stage` derive
a node's needs from `stages.STAGES`: a per-part stage of part k waits for part k of the per-part
stages it names and for every node of the others. So the ideas of a part are listed while the
gap search runs, and part 1 goes on to its spoken form, its cards and its audio while part 2 is
still being verified, instead of every stage waiting for its slowest part (ADR-0006).

Control stays in code: the graph is fixed before anything runs, and no model chooses a node
(ADR-0003). Nodes run in a pool of worker threads; a node marked `main` runs in the thread that
called `run` (the local voice and speech models are loaded and used in that thread only).
The first node that fails stops new nodes from starting; the nodes already running finish, and
`run` raises the failure."""
from __future__ import annotations

import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Hashable

from . import stages


@dataclass
class Node:
    key: Hashable
    function: Callable[[], object]
    needs: tuple
    stage: str | None = None  # the stage the tracker shows while the node runs
    main: bool = False  # run in the thread that called run()
    dependents: list = field(default_factory=list)


class Flow:
    def __init__(self, workers: int = 4, tracker: "stages.Tracker | None" = None):
        self.workers = max(1, workers)
        self.tracker = tracker
        self.nodes: dict[Hashable, Node] = {}
        self.results: dict[Hashable, object] = {}
        self.order: list[Hashable] = []  # the order nodes finished in (for tests and logs)
        self._cond = threading.Condition()

    def __getitem__(self, key):
        return self.results[key]

    def __contains__(self, key) -> bool:
        return key in self.nodes

    def add(self, key, function: Callable[[], object], needs=(), stage: str | None = None,
            main: bool = False) -> Hashable:
        """A node that runs `function()` once every node of `needs` is done. Needs must be
        added first, so the graph cannot have a cycle."""
        if key in self.nodes:
            raise ValueError(f"node {key!r} is added twice")
        if stage is not None and stage not in stages.BY_NAME:
            raise ValueError(f"{stage} is not a stage of stages.STAGES")
        missing = [n for n in needs if n not in self.nodes]
        if missing:
            raise ValueError(f"node {key!r} needs {missing}, which are not added before it")
        self.nodes[key] = Node(key, function, tuple(dict.fromkeys(needs)), stage, main)
        return key

    def of_stage(self, stage: str) -> list[Hashable]:
        """The nodes added so far for a stage (all its parts, for a per-part stage)."""
        return [k for k, n in self.nodes.items() if n.stage == stage]

    def add_part(self, stage: str, part: str, function, extra=(), main: bool = False):
        """The node (stage, part), needing what `stages.part_needs` says: part `part` of the
        per-part stages, every node of the whole-lecture stages. A stage that has no node in
        this run (done before the flow, or turned off) is not waited for."""
        same, whole = stages.part_needs(stage)
        needs = [(n, part) for n in same if (n, part) in self.nodes]
        needs += [k for n in whole for k in self.of_stage(n)]
        return self.add((stage, part), function, [*needs, *extra], stage, main)

    def add_stage(self, stage: str, function, key=None, extra=(), main: bool = False):
        """A whole-lecture node of `stage`, needing every node of the stages it needs."""
        needs = [k for n in stages.BY_NAME[stage].needs for k in self.of_stage(n)]
        return self.add(key or stage, function, [*needs, *extra], stage, main)

    def run(self) -> dict:
        """Run every node; return {key: result}. Blocks until all are done, running the `main`
        nodes in this thread."""
        pending = {k: set(n.needs) for k, n in self.nodes.items()}
        for node in self.nodes.values():
            node.dependents = []
        for key, node in self.nodes.items():
            for need in node.needs:
                self.nodes[need].dependents.append(key)
        left = {}  # stage -> nodes of that stage not done yet
        for node in self.nodes.values():
            if node.stage:
                left[node.stage] = left.get(node.stage, 0) + 1
        state = {"inflight": 0, "error": None}
        local: deque = deque()
        pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="flow")

        def launch(key) -> None:  # called with the condition held
            state["inflight"] += 1
            if self.nodes[key].main:
                local.append(key)
                self._cond.notify_all()
            else:
                pool.submit(execute, key)

        def execute(key) -> None:
            node = self.nodes[key]
            if node.stage and self.tracker:
                self.tracker.start(node.stage)
            try:
                result, error = node.function(), None
            except BaseException as exc:  # handed to run(), which raises it
                result, error = None, exc
            with self._cond:
                state["inflight"] -= 1
                if error is not None:
                    if state["error"] is None:
                        state["error"] = error
                else:
                    self.results[key] = result
                    self.order.append(key)
                    if node.stage:
                        left[node.stage] -= 1
                        if left[node.stage] == 0 and self.tracker:
                            self.tracker.stop(node.stage)
                    if state["error"] is None:
                        for dependent in node.dependents:
                            pending[dependent].discard(key)
                            if not pending[dependent]:
                                launch(dependent)
                self._cond.notify_all()

        try:
            with self._cond:
                for key in [k for k, needs in pending.items() if not needs]:
                    launch(key)
            while True:
                with self._cond:
                    while not (local and state["error"] is None) and state["inflight"]:
                        if local:  # a failure elsewhere: the waiting main nodes never run
                            state["inflight"] -= len(local)
                            local.clear()
                            continue
                        self._cond.wait()
                    if not (local and state["error"] is None):
                        break
                    key = local.popleft()
                execute(key)
        finally:
            pool.shutdown(wait=True)
        if state["error"] is not None:
            raise state["error"]
        unrun = [k for k in self.nodes if k not in self.results]
        if unrun:  # cannot happen: needs are added before the nodes that need them
            raise RuntimeError(f"nodes never ran: {unrun}")
        return self.results
