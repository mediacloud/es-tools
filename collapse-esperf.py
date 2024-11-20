"""
Phil Budne @ Media Cloud
Started October 23, 2024

Take Elasticsearch "profile" response section and output for
consumption by flamegraph.pl

See: https://github.com/brendangregg/FlameGraph
and https://www.brendangregg.com/flamegraphs.html

Reads one or more files for profiling across multiple operations.

Allows summing across all clusters, nodes, indexes or shards.

Takes JSON input file names on command line, else reads a single file
from stdin.  Always outputs to stdout for piping to flamegraph.pl

Input data is expected to be JSON returned under "profile", when
"profile" is set to true in request.

Each "sample" == 1ns
Shards are shown as sNN
Nodes shown as generated node ids.

Thought of writing this in Perl for a femptosecond (since most
flamegraph tools are Perl), but I haven't started anything in Perl
recreationally in over 19 years...
"""

import argparse
import enum
import json
import os
import sys
from collections import Counter
from typing import Any, TextIO


class Detail(enum.Enum):
    NONE = 0  # don't show shards
    INDEX = 1  # don't show shards
    SHARD = 2  # don't show nodes
    NODE = 3  # don't show clusters
    CLUSTER = 4  # show clusters, nodes, indices, shards


class LabelContext:
    def __init__(self, cperf: "CollapseESPerf", label: str):
        self.cperf = cperf
        self.label = label

    def __enter__(self) -> "LabelContext":
        self.cperf._push(self.label)
        return self

    def __exit__(self, type_: Any, value: Any, traceback: Any) -> None:
        self.cperf._pop()


class CollapseESPerf:
    def __init__(self, detail: Detail, use_descr: bool, breakdown: bool):
        # options, set for all files
        self.detail = detail
        self.use_descr = use_descr
        self.breakdown = breakdown

        # reset for each shard
        self.stack: list[str] = []

        # summed over all shards in all queries in all files;
        # flamegraphs are usually done with time based sampling.
        # Here, each "sample" is in a reported nanosecond of runtime.
        self.samples: Counter[str] = Counter()

    def _reset_stack(self) -> None:
        self.stack = []

    def _push(self, name: str) -> None:
        self.stack.append(name)

    def _pop(self) -> None:
        self.stack.pop()

    def _record_node(self, node: dict[str, Any]) -> None:
        """
        record (sampling) samples for a node with time_in_nanos
        and possibly a "breakdown"
        """
        if self.breakdown and "breakdown" in node:
            for name, nanos in node["breakdown"].items():
                if nanos:
                    with self._label(name):
                        self._record_nanos(nanos)
        else:
            self._record_nanos(node["time_in_nanos"])

    def _record_nanos(self, nanos: int) -> None:
        # the one place that formats the stack
        key = ";".join(self.stack)
        self.samples[key] += nanos

    def _label(self, name: str) -> LabelContext:
        return LabelContext(self, name)

    def _query(self, qn: dict[str, Any]) -> None:
        # each query node has:
        # type, description, time_in_nanos, breakdown, children.

        if self.use_descr:
            # Just truncate description for now (whole thing
            # causes browser heartburn), and contains both fields
            # AND values which creates noise.  Need to sanitize so
            # field names present, but values are not!
            label = qn["description"][:100]
        else:
            # gives VERY dry output (just node types)
            label = qn["type"]

        with self._label(label):
            for child in qn.get("children", []):
                self._query(child)

            self._record_node(qn)
        # end of _query

    def _coll(self, cn: dict[str, Any]) -> None:
        # collector nodes have: name, reason, time_in_nanos, children

        # reason is "plain english" description of class name
        # name is class name?
        with self._label(cn["reason"]):
            for child in cn.get("children", []):
                self._coll(child)
            self._record_node(cn)
        # end of _coll

    def _aggs(self, an: dict[str, Any]) -> None:
        # aggregations nodes have: type, description (agg name)
        #    time_in_nanos, breakdown, debug

        with self._label(an["description"]):  # aggregation name
            for child in an.get("children", []):
                self._aggs(child)
            self._record_node(an)
        # end of _aggs

    def collapse(self, stream: TextIO) -> None:
        p = json.load(stream)

        # handle raw query response
        if "took" in p and "profile" in p:
            p = p["profile"]

        # top level has list "shards", each of which has:
        # id (concatenated node_id, index, shard number)
        # node_id
        # shard_id
        # index
        # cluster

        for shard in p["shards"]:
            self._reset_stack()

            # prepare the foundation, to order.
            # (should be the only place that calls _push directly).
            if detail.value >= Detail.CLUSTER.value:
                self._push(shard["cluster"])
            if detail.value >= Detail.NODE.value:
                self._push(shard["node_id"])
            if detail.value >= Detail.INDEX.value:
                self._push(shard["index"])
            if detail.value >= Detail.SHARD.value:
                self._push(f"s{shard['shard_id']}")  # format shard as sNN

            with self._label("search"):
                for search in shard["searches"]:  # list
                    """
                    here with dict with 'query', 'rewrite_time', 'collector', 'aggregations'
                    """

                    # maybe prepend a digit to rewrite_time, coll, aggs to force the
                    # order they appear in (flamegraph.pl sorts the input by path,
                    # for consistency.

                    with self._label("rewrite"):  # add digit?
                        self._record_nanos(search["rewrite_time"])

                    with self._label("query"):  # add digit?
                        for q in search["query"]:  # list
                            self._query(q)

                    with self._label("collector"):  # add digit?
                        for cn in search["collector"]:  # list
                            self._coll(cn)

            with self._label("aggregations"):
                for an in shard.get("aggregations", []):  # list
                    self._aggs(an)

    def dump(self, output: TextIO) -> None:
        for key, sum in self.samples.items():
            output.write(f"{key} {sum}\n")


ap = argparse.ArgumentParser(
    "esperf-collapse",
    description="prepare Elasticsearch 'profile' data for flamegraph.pl",
)

levels = [level.name.lower() for level in Detail]
ap.add_argument("--detail", choices=levels, default="cluster", help="level of detail")
ap.add_argument(
    "--descr",
    action="store_true",
    default=False,
    help="display (truncated) description; may contain query data!",
)
ap.add_argument(
    "--no-breakdown",
    dest="breakdown",
    action="store_false",
    default=True,
    help="report breakdown times",
)
ap.add_argument(
    "--output", "-o", metavar="OUTPUT_FILE", dest="output", help="set output filename"
)
ap.add_argument("files", nargs="*", default=None)

args = ap.parse_args()
detail = getattr(Detail, args.detail.upper())

if args.descr and os.environ.get("ESPERF_NO_WARNING", None) in (None, ""):
    sys.stderr.write("WARNING! graphs may reveal query parameters!\n")

c = CollapseESPerf(detail, args.descr, args.breakdown)

if args.files:
    for fname in args.files:
        with open(fname) as f:
            c.collapse(f)
else:
    # read a single file from stdin
    c.collapse(sys.stdin)

if args.output:
    with open(args.output, "w") as f:
        c.dump(f)
else:
    c.dump(sys.stdout)
